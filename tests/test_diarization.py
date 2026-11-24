import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ucheba import diarization
from ucheba.diarization import SpeakerTurn, attribute_speakers, suggest_lecturer
from ucheba.models import Segment


def test_assignment_keeps_order_and_leaves_ambiguous_segments_unknown():
    segments = [
        Segment(start=15, end=19, text="Задание."),
        Segment(start=1, end=6, text="Добрый день."),
        Segment(start=8, end=12, text="Две реплики."),
        Segment(start=22, end=24, text="Шум."),
        Segment(start=2, end=4, text="Уточнение.", speaker="Ольга"),
    ]
    turns = [SpeakerTurn(10, 20, "B"), SpeakerTurn(0, 10, "A")]
    result = attribute_speakers(segments, turns)

    assert [item.speaker for item in result.segments] == [
        "Говорящий 2",
        "Говорящий 1",
        None,
        None,
        "Ольга",
    ]
    assert [item.text for item in result.segments] == [item.text for item in segments]
    assert [item.speaker for item in result.turns] == ["Говорящий 1", "Говорящий 2"]
    assert segments[0].speaker is None


def test_lecturer_hint_uses_words_and_duration():
    long_monologue = Segment(start=0, end=100, text="Я рассказал о проекте.", speaker="Студент")
    assert suggest_lecturer([long_monologue]) is None
    segments = [
        long_monologue,
        Segment(start=100, end=126, text="Рассмотрим основы теории.", speaker="A"),
        Segment(start=126, end=147, text="Обратите внимание на знак.", speaker="A"),
        Segment(start=147, end=157, text="Домашнее задание?", speaker="Б"),
    ]
    assert suggest_lecturer(segments) == "A"
    segments.append(Segment(start=157, end=175, text="Задачу выполните дома.", speaker="Б"))
    assert suggest_lecturer(segments) is None


def test_diarize_uses_local_model_and_private_audio(monkeypatch, tmp_path):
    media = tmp_path / "lecture.mp4"
    media.touch()
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.yaml").touch()
    files = []

    class Pipeline:
        @classmethod
        def from_pretrained(cls, path):
            assert path == str(model)
            return cls()

        def __call__(self, filename, *, hook):
            assert Path(filename) == files[0]
            hook("embeddings", None, completed=1, total=1)
            return SimpleNamespace(
                exclusive_speaker_diarization=SimpleNamespace(
                    itertracks=lambda **_kwargs: [(SimpleNamespace(start=0, end=10), 0, "B")]
                )
            )

    def extract(_ffmpeg, source, audio, start, duration, _cancelled):
        assert source == media
        assert start == 0
        assert duration == 10
        files.append(audio)
        audio.touch()

    monkeypatch.setenv("PYANNOTE_METRICS_ENABLED", "1")
    monkeypatch.setitem(sys.modules, "pyannote.audio", SimpleNamespace(Pipeline=Pipeline))
    monkeypatch.setattr(diarization, "_duration", lambda *_args: 10)
    monkeypatch.setattr(diarization, "_tool", lambda name: name)
    monkeypatch.setattr(diarization, "_extract_audio", extract)
    result = diarization.diarize(media, [Segment(start=0, end=8, text="Введение.")], model)
    assert result.segments[0].speaker == "Говорящий 1"
    assert not files[0].exists()
    assert diarization.os.environ["PYANNOTE_METRICS_ENABLED"] == "0"


def test_long_recording_skips_loading_model(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").touch()
    media = tmp_path / "video.mp4"
    media.touch()
    monkeypatch.setattr(diarization, "_duration", lambda *_args: 3_601)
    with pytest.raises(ValueError, match="до одного часа"):
        diarization.diarize(media, [], tmp_path)
