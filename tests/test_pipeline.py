from pathlib import Path

import pytest
from docx import Document

from ucheba.imports import load_transcript
from ucheba.pipeline import process
from ucheba.queue import QueueStore


def test_import_export_and_recovery(tmp_path: Path) -> None:
    source = tmp_path / "Механика.srt"
    source.write_text("1\n01:00:01,500 --> 01:00:04,000\nЗакон Дарси.\n", encoding="utf-8")
    queue = QueueStore(tmp_path / "queue.db")
    job = queue.add(source, tmp_path / "results", model="small", device="cpu", cloud=False)
    assert queue.claim_next() is not None
    assert queue.claim_next() is None
    queue.recover()
    assert queue.claim_next() is not None
    result = process(job, None, lambda message: None, lambda: False)
    doc = Document(result / "01_Полный_текст_лекции.docx")
    assert any("[01:00:01] Закон Дарси." in p.text for p in doc.paragraphs)
    assert load_transcript(result / "transcript.json")[0].start == 3601.5
    assert process(job, None, lambda message: None, lambda: False) == result
    queue.finish(job.id, "succeeded", result=result)
    assert queue.get(job.id).result == result


def test_cancelled_job_never_claimed(tmp_path: Path) -> None:
    queue = QueueStore(tmp_path / "queue.db")
    job = queue.add(tmp_path / "in.txt", tmp_path, model="small", device="cpu", cloud=False)
    queue.cancel(job.id)
    assert queue.claim_next() is None
    queue.retry(job.id)
    assert queue.claim_next() is not None


def test_invalid_subtitle_interval(tmp_path: Path) -> None:
    source = tmp_path / "invalid.vtt"
    source.write_text("WEBVTT\n\n00:02.000 --> 00:01.000\nТекст", encoding="utf-8")
    with pytest.raises(ValueError, match="некорректные"):
        load_transcript(source)


def test_cloud_failure_leaves_word_and_source_replacement_needs_consent(tmp_path: Path) -> None:
    from ucheba.pipeline import partial_result

    source = tmp_path / "Лекция.txt"
    source.write_text("Исходная лекция", encoding="utf-8")
    queue = QueueStore(tmp_path / "queue.db")
    job = queue.add(source, tmp_path / "results", model="small", device="cpu", cloud=True)
    with pytest.raises(ValueError, match="Добавьте ключ"):
        process(job, None, lambda message: None, lambda: False)
    partial = partial_result(job)
    assert partial is not None
    assert (partial / "01_Полный_текст_лекции.docx").is_file()
    source.write_text("Совершенно другая запись", encoding="utf-8")
    with pytest.raises(ValueError, match="Исходная запись изменилась"):
        process(job, "fake-key", lambda message: None, lambda: False)


def test_queue_keeps_optional_processing_settings(tmp_path: Path) -> None:
    queue = QueueStore(tmp_path / "queue.db")
    model = tmp_path / "community"
    added = queue.add(
        tmp_path / "lecture.mp4",
        tmp_path / "results",
        model="base",
        device="cpu",
        cloud=False,
        slides=True,
        speaker_model=model,
    )
    claimed = QueueStore(tmp_path / "queue.db").claim_next()
    assert claimed is not None and claimed.id == added.id
    assert claimed.slides and claimed.speaker_model == model


def test_speaker_review_keeps_source_and_students(tmp_path: Path) -> None:
    from ucheba.documents import write_json
    from ucheba.speakers import save_review

    original = tmp_path / "transcript.json"
    write_json(
        original,
        [
            {"text": "Выполните расчёт", "start": 3, "end": 4, "speaker": "A"},
            {"text": "Какие единицы?", "start": 7, "end": 8, "speaker": "B"},
            {"text": "Фон", "start": 10, "end": 11, "speaker": "C"},
        ],
    )
    before = original.read_bytes()
    result = save_review(
        tmp_path,
        {"A": ("Петрова", "lecturer"), "B": ("Студент", "student"), "C": ("Фон", "ignore")},
    )
    text = "\n".join(p.text for p in Document(str(result)).paragraphs)
    assert "Петрова" in text and "Какие единицы?" in text and "Фон" not in text
    assert original.read_bytes() == before


def test_diarization_failure_preserves_transcript(tmp_path: Path, monkeypatch) -> None:
    from ucheba.models import Segment
    from ucheba.pipeline import partial_result

    source = tmp_path / "lecture.mp4"
    source.touch()
    queue = QueueStore(tmp_path / "profile" / "queue.db")
    job = queue.add(
        source,
        tmp_path / "shared",
        model="base",
        device="cpu",
        cloud=False,
        speaker_model=tmp_path / "model",
    )
    monkeypatch.setattr(
        "ucheba.transcription.transcribe",
        lambda *a, **kw: [
            Segment(start=0, end=1, text="Распознанная лекция"),
        ],
    )

    def fail(*args, **kwargs):
        raise ValueError("Длительность превышает предел.")

    monkeypatch.setattr("ucheba.diarization.diarize", fail)
    with pytest.raises(ValueError, match="Длительность"):
        process(job, None, lambda _: None, lambda: False)
    partial = partial_result(job)
    assert partial is not None and (partial / "01_Полный_текст_лекции.docx").is_file()
    assert partial.is_relative_to(tmp_path / "profile")
    assert not (job.output / ".ucheba-checkpoints").exists()


def test_cloud_ignores_untrusted_checkpoint_on_results_share(tmp_path: Path, monkeypatch) -> None:
    from ucheba.models import Analysis

    source = tmp_path / "source.txt"
    source.write_text("Согласованный текст", encoding="utf-8")
    queue = QueueStore(tmp_path / "profile" / "queue.db")
    job = queue.add(source, tmp_path / "shared", model="base", device="cpu", cloud=True)
    untrusted = job.output / ".ucheba-checkpoints" / job.id / "Расшифровка"
    untrusted.mkdir(parents=True)
    (untrusted / "transcript.json").write_text("Несогласованный текст", encoding="utf-8")
    (untrusted / "metadata.json").write_text("{}", encoding="utf-8")

    def fake_analysis(segments, *_args, **_kwargs):
        assert [item.text for item in segments] == ["Согласованный текст"]
        return Analysis()

    monkeypatch.setattr("ucheba.analysis.analyze", fake_analysis)
    output = process(job, "fake-key", lambda _: None, lambda: False)
    assert (output / "02_Конспект_лекции.docx").is_file()


def test_selected_word_preset_survives_queue_and_export(tmp_path: Path) -> None:
    source = tmp_path / "lecture.txt"
    source.write_text("Академический конспект", encoding="utf-8")
    queue = QueueStore(tmp_path / "queue.db")
    job = queue.add(
        source,
        tmp_path / "results",
        model="base",
        device="cpu",
        cloud=False,
        template="compact",
    )
    assert queue.get(job.id).template == "compact"
    folder = process(job, None, lambda _: None, lambda: False)
    doc = Document(str(folder / "01_Полный_текст_лекции.docx"))
    assert doc.styles["Normal"].font.name == "Calibri"
    assert doc.styles["Normal"].font.size.pt == 10


def test_builtin_lecturer_label_is_persisted_and_exported(tmp_path: Path, monkeypatch) -> None:
    import json

    from ucheba.diarization import DiarizationResult
    from ucheba.models import Segment

    source = tmp_path / "lecture.mp4"
    source.touch()
    queue = QueueStore(tmp_path / "profile" / "queue.db")
    job = queue.add(
        source,
        tmp_path / "reports",
        model="base",
        device="cpu",
        cloud=False,
        auto_speakers=True,
    )
    assert queue.get(job.id).auto_speakers
    speech = Segment(start=42, end=51, text="Запишите задание.")
    teacher = speech.model_copy(update={"speaker": "Говорящий 1"})
    monkeypatch.setattr("ucheba.transcription.transcribe", lambda *args, **kwargs: [speech])
    monkeypatch.setattr(
        "ucheba.builtin_diarization.diarize_builtin",
        lambda *args, **kwargs: DiarizationResult([teacher], [], "Говорящий 1"),
    )
    result = process(job, None, lambda _: None, lambda: False)
    metadata = json.loads((result / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["lecturer_candidate"] == teacher.speaker
    report = Document(str(result / "05_Вероятная_речь_преподавателя.docx"))
    assert any("[00:00:42] Запишите задание." in p.text for p in report.paragraphs)


def test_builtin_failure_keeps_complete_transcript(tmp_path: Path, monkeypatch) -> None:
    import json

    from ucheba.models import Segment

    source = tmp_path / "lecture.mp4"
    source.touch()
    queue = QueueStore(tmp_path / "profile" / "queue.db")
    job = queue.add(
        source, tmp_path / "reports", model="base", device="cpu", cloud=False, auto_speakers=True
    )
    monkeypatch.setattr(
        "ucheba.transcription.transcribe",
        lambda *args, **kwargs: [
            Segment(start=0, end=5, text="Текст лекции"),
        ],
    )

    def fail(*args, **kwargs):
        raise RuntimeError("Разделение голосов недоступно.")

    monkeypatch.setattr("ucheba.builtin_diarization.diarize_builtin", fail)
    result = process(job, None, lambda _: None, lambda: False)
    assert (result / "01_Полный_текст_лекции.docx").is_file()
    metadata = json.loads((result / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["speaker_warning"] == "Разделение голосов недоступно."
    assert metadata["lecturer_candidate"] is None


def test_auto_chooses_cpu_before_loading_model_if_cuda_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    from ucheba.models import Segment

    source = tmp_path / "record.mp4"
    source.touch()
    queue = QueueStore(tmp_path / "profile" / "queue.db")
    job = queue.add(source, tmp_path / "reports", model="base", device="auto", cloud=False)
    devices = []

    def transcribe(*args, device, **kwargs):
        devices.append(device)
        return [Segment(start=0, end=1, text="Тестовая лекция")]

    monkeypatch.setattr("ucheba.pipeline.missing_cuda_libraries", lambda: ("cublas64_12.dll",))
    monkeypatch.setattr("ucheba.transcription.transcribe", transcribe)
    activity = []
    process(job, None, activity.append, lambda: False)
    assert devices == ["cpu"]
    assert any("CUDA" in item and "CPU" in item for item in activity)


def test_output_access_is_checked_before_recognition(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "record.mp4"
    source.touch()
    blocked = tmp_path / "report"
    blocked.write_text("existing data", encoding="utf-8")
    queue = QueueStore(tmp_path / "profile" / "queue.db")
    job = queue.add(source, blocked, model="base", device="cpu", cloud=False)

    def should_not_transcribe(*args, **kwargs):
        pytest.fail("Нельзя запускать распознавание при недоступном выводе.")

    monkeypatch.setattr("ucheba.transcription.transcribe", should_not_transcribe)
    with pytest.raises(ValueError, match="Папка результатов недоступна для записи"):
        process(job, None, lambda _: None, lambda: False)
    assert blocked.read_text(encoding="utf-8") == "existing data"
