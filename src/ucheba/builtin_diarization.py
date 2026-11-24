"""Локальная диаризация с моделями ONNX из комплекта приложения."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

import httpx

from .diarization import DiarizationResult, SpeakerTurn, attribute_speakers
from .models import Segment
from .transcription import _duration, _extract_audio, _read_pcm, _tool
from .updates import UpdateError, _asset_bytes, _client

_MAX_DURATION = 3600
# A higher cosine-distance limit reduces fragmentation across microphone changes.
_CLUSTER_DISTANCE_THRESHOLD = 0.7
_EMBEDDING_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
    "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
)
_EMBEDDING_SHA256 = "f1ee61be04000b50392a63442e5dd9feaea7e81dddd494d3647cf1c3d88a1498"
_EMBEDDING_SIZE = 39_593_762


def _download_embedding(
    directory: Path,
    progress: Callable[[str], None],
    cancelled: Callable[[], bool],
    *,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    destination = directory / "embedding.onnx"
    pending = None
    try:
        if destination.is_file() and not destination.is_symlink():
            with destination.open("rb") as stream:
                if (
                    os.fstat(stream.fileno()).st_size == _EMBEDDING_SIZE
                    and hashlib.file_digest(stream, "sha256").hexdigest() == _EMBEDDING_SHA256
                ):
                    return destination
        if cancelled():
            raise InterruptedError("Загрузка модели разделения голосов отменена.")
        progress("Загрузка модели разделения голосов с GitHub")
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with (
            _client(transport) as client,
            NamedTemporaryFile(dir=directory, suffix=".part", delete=False) as stream,
        ):
            pending = Path(stream.name)
            for chunk in _asset_bytes(client, _EMBEDDING_URL, _EMBEDDING_SIZE):
                if cancelled():
                    raise InterruptedError("Загрузка модели разделения голосов отменена.")
                digest.update(chunk)
                stream.write(chunk)
            if stream.tell() != _EMBEDDING_SIZE or digest.hexdigest() != _EMBEDDING_SHA256:
                raise RuntimeError("Контрольная сумма модели разделения голосов не совпала.")
        pending.replace(destination)
    except InterruptedError:
        raise
    except (OSError, httpx.HTTPError, UpdateError) as exc:
        raise RuntimeError(
            "Не удалось загрузить модель разделения голосов. Проверьте доступ к GitHub."
        ) from exc
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return destination


def _model_paths(
    model_dir: Path | None,
    progress: Callable[[str], None] = lambda message: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> tuple[Path, Path]:
    package_dir = Path(__file__).resolve().parent
    candidates = (
        [model_dir]
        if model_dir is not None
        else [
            package_dir / "models" / "diarization",
            package_dir.parent.parent / "build" / "diarization-models",
        ]
    )
    for directory in candidates:
        segmentation = directory / "segmentation.onnx"
        embedding = directory / "embedding.onnx"
        if segmentation.is_file() and embedding.is_file():
            return segmentation.resolve(), embedding.resolve()
        if (
            model_dir is None
            and segmentation.is_file()
            and (directory / "embedding-download.txt").is_file()
        ):
            profile = Path(os.environ.get("LOCALAPPDATA") or Path.home())
            return segmentation.resolve(), _download_embedding(
                profile / "UchebaAI" / "SpeakerModels", progress, cancelled
            )
    raise RuntimeError(
        "В комплекте не найдены модели разделения голосов. Восстановите установку «Учёба AI»."
    )


def diarize_builtin(
    source: Path,
    segments: Sequence[Segment],
    *,
    progress: Callable[[str], None] = lambda message: None,
    cancelled: Callable[[], bool] = lambda: False,
    model_dir: Path | None = None,
) -> DiarizationResult:
    """Определить говорящих локально и предложить вероятного преподавателя."""
    if cancelled():
        raise InterruptedError("Разделение голосов отменено.")
    duration = _duration(source, cancelled)
    if duration > _MAX_DURATION:
        raise ValueError("Разделение голосов поддерживает записи длительностью до одного часа.")
    segmentation, embedding = _model_paths(model_dir, progress, cancelled)

    try:
        import sherpa_onnx
    except (ImportError, OSError) as exc:
        raise RuntimeError("Не удалось загрузить встроенный модуль разделения голосов.") from exc

    threads = min(os.cpu_count() or 1, 4)
    progress("Подготовка аудио для разделения голосов")
    with TemporaryDirectory(prefix="ucheba-speakers-") as temporary:
        audio = Path(temporary) / "audio.wav"
        _extract_audio(_tool("ffmpeg"), source, audio, 0, duration, cancelled)
        samples = _read_pcm(audio)

    segmentation_config = sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
        pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(segmentation)),
        num_threads=threads,
    )
    embedding_config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(embedding), num_threads=threads
    )
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=segmentation_config,
        embedding=embedding_config,
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=-1, threshold=_CLUSTER_DISTANCE_THRESHOLD
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    last_step = -10

    def notify(processed: int, total: int) -> int:
        nonlocal last_step
        if cancelled():
            return 1
        percent = min(100, round(100 * processed / total)) if total > 0 else 0
        if percent >= last_step + 10:
            last_step = percent
            progress(f"Определение говорящих: {percent}%")
        return 0

    try:
        progress("Определение говорящих локальной моделью")
        engine = sherpa_onnx.OfflineSpeakerDiarization(config)
        output = engine.process(samples, notify)
    except RuntimeError as exc:
        if cancelled():
            raise InterruptedError("Разделение голосов отменено.") from exc
        raise RuntimeError("Встроенная модель не смогла разделить говорящих.") from exc
    if cancelled():
        raise InterruptedError("Разделение голосов отменено.")
    turns = [
        SpeakerTurn(turn.start, turn.end, str(turn.speaker)) for turn in output.sort_by_start_time()
    ]
    return attribute_speakers(segments, turns)
