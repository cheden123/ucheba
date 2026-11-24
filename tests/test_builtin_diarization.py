import hashlib
from pathlib import Path

import httpx
import pytest

from ucheba import builtin_diarization as engine
from ucheba.builtin_diarization import _model_paths, diarize_builtin
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
    monkeypatch.setattr("ucheba.builtin_diarization._duration", lambda *_: 3601.0)
    with pytest.raises(ValueError, match="до одного часа"):
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
