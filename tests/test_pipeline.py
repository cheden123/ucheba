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
