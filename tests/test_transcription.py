import json
import math
import shutil
import struct
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from ucheba import transcription


def test_bounded_audio_and_resume(monkeypatch, tmp_path):
    source = tmp_path / "Лекция.mp4"
    source.write_bytes(b"source")
    checkpoint = tmp_path / "checkpoint.json"
    extracts = []
    model_calls = []
    interrupted = False

    def command(arguments, _cancelled, _timeout):
        if arguments[0] == "ffprobe":
            return json.dumps({"format": {"duration": 601}, "streams": [{"index": 0}]})
        if interrupted:
            raise InterruptedError
        seek = float(arguments[arguments.index("-ss") + 1])
        length = float(arguments[arguments.index("-t") + 1])
        extracts.append((seek, length))
        return ""

    class WhisperModel:
        def __init__(self, model, **kwargs):
            model_calls.append((model, kwargs))

        def transcribe(self, _audio, *, language, vad_filter, initial_prompt):
            nonlocal interrupted
            assert language == "ru"
            assert vad_filter
            assert initial_prompt == "Тензор"
            if len(extracts) == 1 and len(model_calls) == 1:
                interrupted = True
            offset = extracts[-1][0]
            rows = [
                SimpleNamespace(start=t - offset, end=t + 0.2 - offset, text=str(t))
                for t in (1, 299, 301, 599, 600.2)
                if offset <= t < offset + extracts[-1][1]
            ]
            return iter(rows), None

    monkeypatch.setattr(transcription, "_tool", lambda name: name)
    monkeypatch.setattr(transcription, "_run", command)
    monkeypatch.setattr(transcription, "_read_pcm", lambda _path: np.ones(16_000, dtype=np.float32))
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=WhisperModel))
    with pytest.raises(InterruptedError):
        transcription.transcribe(source, checkpoint, "large-v3-turbo", glossary="Тензор")
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["next_chunk"] == 1
    interrupted = False
    result = transcription.transcribe(source, checkpoint, "large-v3-turbo", glossary="Тензор")
    assert extracts == [(0, 308), (292, 309), (592, 9)]
    assert [segment.start for segment in result] == pytest.approx([1, 299, 301, 599, 600.2])
    assert model_calls == [("turbo", {"device": "cpu", "compute_type": "int8"})] * 2
    assert (
        transcription.transcribe(source, checkpoint, "large-v3-turbo", glossary="Тензор") == result
    )
    assert len(model_calls) == 2


def test_source_and_settings_change_invalidates_checkpoint(tmp_path):
    path = tmp_path / "checkpoint.json"
    signature = {"source": "лекция", "size": 1024}
    transcription._save_checkpoint(path, signature, 1, [])
    assert transcription._load_checkpoint(path, signature, 1) == (1, [])
    assert transcription._load_checkpoint(path, {**signature, "size": 4096}, 1) == (0, [])


def test_bundled_tools_are_preferred(monkeypatch, tmp_path):
    exe = tmp_path / "UchebaAI.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.touch()
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert Path(transcription._tool("ffmpeg")) == ffmpeg


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Для проверки аудиоконвейера нужны FFmpeg и FFprobe в PATH",
)
def test_ffmpeg_extracts_mono_pcm_for_whisper(monkeypatch, tmp_path):
    source = tmp_path / "Аудио 2 канала.wav"
    frames = bytearray()
    for sample in range(22_050):
        value = int(10_000 * math.sin(2 * math.pi * 440 * sample / 22_050))
        frames.extend(struct.pack("<hh", value, value))
    with wave.open(str(source), "wb") as audio:
        audio.setparams((2, 2, 22_050, 0, "NONE", "not compressed"))
        audio.writeframes(frames)

    class WhisperModel:
        def __init__(self, _model, **_options):
            pass

        def transcribe(self, samples, **options):
            assert options["vad_filter"]
            assert samples.shape == (16_000,)
            assert samples.dtype == np.float32
            assert 0 < samples.max() < 1
            assert -1 < samples.min() < 0
            return iter([SimpleNamespace(start=0, end=1, text="Тест")]), None

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=WhisperModel))
    result = transcription.transcribe(source, tmp_path / "checkpoint.json")
    assert [segment.text for segment in result] == ["Тест"]
