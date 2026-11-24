import hashlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from ucheba import builtin_diarization as engine
from ucheba.builtin_diarization import _model_paths, diarize_builtin
from ucheba.diarization import SpeakerTurn
from ucheba.models import Segment


def test_selects_complete_model_directory(tmp_path: Path) -> None:
    (tmp_path / "segmentation.onnx").touch()
    with pytest.raises(RuntimeError, match="не найдены модели"):
        _model_paths(tmp_path)
    (tmp_path / "embedding.onnx").touch()
    assert _model_paths(tmp_path) == (
        tmp_path / "segmentation.onnx",
        tmp_path / "embedding.onnx",
    )


def test_respects_cancellation_before_reading_files(tmp_path: Path) -> None:
    with pytest.raises(InterruptedError):
        diarize_builtin(tmp_path / "absent.mp4", [], cancelled=lambda: True)


def test_refuses_long_recordings_before_preparing_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "segmentation.onnx").touch()
    (tmp_path / "embedding.onnx").touch()
    monkeypatch.setattr("ucheba.builtin_diarization._duration", lambda *_: 28_801.0)
    with pytest.raises(ValueError, match="до восьми часов"):
        diarize_builtin(
            tmp_path / "lecture.mp4",
            [Segment(start=0, end=1, text="Обратите внимание.")],
            model_dir=tmp_path,
        )


def test_download_verifies_model_and_reuses_verified_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = b"valid ONNX fixture"
    monkeypatch.setattr(engine, "_EMBEDDING_SHA256", hashlib.sha256(model).hexdigest())
    monkeypatch.setattr(engine, "_EMBEDDING_SIZE", len(model))
    requests = []

    def response(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == engine._EMBEDDING_URL
        requests.append(request)
        return httpx.Response(200, content=model)

    transport = httpx.MockTransport(response)
    downloaded = engine._download_embedding(
        tmp_path, lambda _: None, lambda: False, transport=transport
    )
    assert downloaded.read_bytes() == model
    assert (
        engine._download_embedding(tmp_path, lambda _: None, lambda: False, transport=transport)
        == downloaded
    )
    assert len(requests) == 1
    downloaded.write_bytes(b"x" * len(model))
    assert (
        engine._download_embedding(
            tmp_path, lambda _: None, lambda: False, transport=transport
        ).read_bytes()
        == model
    )
    assert len(requests) == 2


def test_invalid_model_bytes_leave_no_model_or_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "_EMBEDDING_SIZE", 4)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"bad!"))
    with pytest.raises(RuntimeError, match="Контрольная сумма"):
        engine._download_embedding(tmp_path, lambda _: None, lambda: False, transport=transport)
    assert not list(tmp_path.iterdir())


def test_voice_linker_retains_unambiguous_matches_and_splits_ambiguity() -> None:
    first = np.array([1.0, 0.0], dtype=np.float32)
    second = np.array([0.0, 1.0], dtype=np.float32)
    confused = np.array([0.7071, 0.7071], dtype=np.float32)
    linker = engine._VoiceLinker()
    turns = [SpeakerTurn(0, 4, "one"), SpeakerTurn(4, 8, "two")]
    initial = linker.link(turns, {"one": first, "two": second})
    matches = linker.link(turns, {"one": second, "two": first})
    assert matches == {"one": initial["two"], "two": initial["one"]}
    ambiguous = linker.link(turns, {"one": confused})
    assert set(ambiguous.values()).isdisjoint(initial.values())


def test_long_recording_only_decodes_one_chunk_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sherpa_onnx

    (tmp_path / "segmentation.onnx").touch()
    (tmp_path / "embedding.onnx").touch()
    monkeypatch.setattr(engine, "_duration", lambda *_: 3600.25)
    monkeypatch.setattr(engine, "_tool", lambda _: "ffmpeg")
    windows = []
    monkeypatch.setattr(
        engine,
        "_extract_audio",
        lambda _, source, audio, start, duration, cancelled: windows.append((start, duration)),
    )
    monkeypatch.setattr(engine, "_read_pcm", lambda _: np.zeros(1, dtype=np.float32))
    local = [SimpleNamespace(start=0.0, end=0.2, speaker=0)]
    output = SimpleNamespace(sort_by_start_time=lambda: local)
    fake = SimpleNamespace(process=lambda *_: output)
    monkeypatch.setattr(sherpa_onnx, "OfflineSpeakerDiarization", lambda _: fake)
    monkeypatch.setattr(sherpa_onnx, "SpeakerEmbeddingExtractor", lambda _: object())
    monkeypatch.setattr(
        engine, "_speaker_vectors", lambda *_: {"0": np.array([1.0, 0.0], dtype=np.float32)}
    )

    segments = [
        Segment(start=starts, end=starts + 0.2, text="Проверка") for starts in (0.0, 900.0, 3600.0)
    ]
    result = diarize_builtin(tmp_path / "lecture.mp4", segments, model_dir=tmp_path)
    assert windows == [(0, 900), (900, 900), (1800, 900), (2700, 900), (3600, 0.25)]
    assert len({seg.speaker for seg in result.segments}) == 1
    assert result.segments[0].speaker == "Говорящий 1"


def test_pools_non_overlapping_short_turns_for_reidentification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = []

    def profile(_, samples):
        captured.append(len(samples))
        return np.array([1.0, 0.0], dtype=np.float32)

    monkeypatch.setattr(engine, "_profile", profile)
    samples = np.zeros(128_000, dtype=np.float32)
    turns = [
        SpeakerTurn(0.0, 2.0, "lecturer"),
        SpeakerTurn(3.0, 5.0, "lecturer"),
        SpeakerTurn(5.0, 6.5, "another"),
    ]
    result = engine._speaker_vectors(object(), samples, turns, lambda: False)
    assert set(result) == {"lecturer"}
    assert captured == [54_400]
