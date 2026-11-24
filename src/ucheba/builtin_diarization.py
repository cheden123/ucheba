"""Локальная диаризация с моделями ONNX из комплекта приложения."""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any

import httpx
import numpy as np
from numpy.typing import NDArray

from .diarization import DiarizationResult, SpeakerTurn, attribute_speakers
from .models import Segment
from .transcription import _duration, _extract_audio, _read_pcm, _tool
from .updates import UpdateError, _asset_bytes, _client

_MAX_DURATION = 8 * 3600
_SHORT_DURATION = 3600
_CHUNK_DURATION = 900
_REIDENTIFICATION_THRESHOLD = 0.68
_REIDENTIFICATION_MARGIN = 0.12
_MIN_CLEAN_SPAN = 1.25
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


Vector = NDArray[np.float32]


def _unit(vector: Vector) -> Vector | None:
    length = float(np.linalg.norm(vector))
    return vector / length if np.isfinite(length) and length > 1e-6 else None


def _clean_spans(turn: SpeakerTurn, turns: Sequence[SpeakerTurn]) -> list[tuple[float, float]]:
    spans = [(turn.start, turn.end)]
    for other in turns:
        if other.speaker == turn.speaker or other.end <= turn.start or other.start >= turn.end:
            continue
        spans = [
            part
            for start, end in spans
            for part in ((start, min(end, other.start)), (max(start, other.end), end))
            if part[1] - part[0] >= _MIN_CLEAN_SPAN
        ]
    return spans


def _profile(extractor: Any, samples: Vector) -> Vector | None:
    stream = extractor.create_stream()
    stream.accept_waveform(16_000, samples)
    stream.input_finished()
    if extractor.is_ready(stream):
        return _unit(np.asarray(extractor.compute(stream), dtype=np.float32))
    return None


def _speaker_vectors(
    extractor: Any, samples: Vector, turns: list[SpeakerTurn], cancelled: Callable[[], bool]
) -> dict[str, Vector]:
    by_speaker: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for turn in turns:
        if turn.end - turn.start >= _MIN_CLEAN_SPAN:
            by_speaker[turn.speaker].extend(_clean_spans(turn, turns))
    profiles: dict[str, Vector] = {}
    for speaker, spans in by_speaker.items():
        vectors = []
        excerpts = []
        for start, end in sorted(spans, key=lambda span: span[1] - span[0], reverse=True)[:4]:
            if cancelled():
                raise InterruptedError("Разделение голосов отменено.")
            margin = min(0.15, (end - start - _MIN_CLEAN_SPAN) / 2)
            first = max(0, round((start + margin) * 16_000))
            last = min(len(samples), round((end - margin) * 16_000), first + 128_000)
            if last - first >= 20_000:
                excerpts.append(samples[first:last])
            if (
                last - first >= 48_000
                and (unit := _profile(extractor, samples[first:last])) is not None
            ):
                vectors.append(unit)
        if not vectors and sum(map(len, excerpts)) >= 48_000:
            # Several brief, clean turns can provide enough evidence together.
            pooled = _profile(extractor, np.concatenate(excerpts)[:128_000])
            if pooled is not None:
                vectors.append(pooled)
        if vectors and (center := _unit(np.mean(vectors, axis=0))) is not None:
            profiles[speaker] = center
    return profiles


class _VoiceLinker:
    def __init__(self) -> None:
        self.gallery: dict[str, Vector] = {}
        self.next_label = 0

    def link(self, turns: list[SpeakerTurn], profiles: dict[str, Vector]) -> dict[str, str]:
        scores = {
            local: sorted(
                (
                    (float(np.dot(profile, prior)), speaker)
                    for speaker, prior in self.gallery.items()
                ),
                reverse=True,
            )
            for local, profile in profiles.items()
        }
        labels = {}
        for local in dict.fromkeys(turn.speaker for turn in turns):
            if ranked := scores.get(local):
                score, target = ranked[0]
                alternative = ranked[1][0] if len(ranked) > 1 else -1
                contender = max(
                    (
                        value
                        for other, matches in scores.items()
                        if other != local
                        for value, known in matches
                        if known == target
                    ),
                    default=-1,
                )
                if (
                    score >= _REIDENTIFICATION_THRESHOLD
                    and score - max(alternative, contender) >= _REIDENTIFICATION_MARGIN
                ):
                    labels[local] = target
                    continue
            labels[local] = str(self.next_label)
            self.next_label += 1
        for local, profile in profiles.items():
            known = labels[local]
            previous = self.gallery.get(known)
            if previous is not None:
                updated = _unit(previous * 0.85 + profile * 0.15)
                if updated is not None:
                    self.gallery[known] = updated
            else:
                self.gallery[known] = profile
        return labels


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
        raise ValueError("Разделение голосов поддерживает записи длительностью до восьми часов.")
    segmentation, embedding = _model_paths(model_dir, progress, cancelled)

    try:
        import sherpa_onnx
    except (ImportError, OSError) as exc:
        raise RuntimeError("Не удалось загрузить встроенный модуль разделения голосов.") from exc

    threads = min(os.cpu_count() or 1, 4)
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
    long_recording = duration > _SHORT_DURATION
    chunk_length = _CHUNK_DURATION if long_recording else _SHORT_DURATION
    turns = []
    linker = _VoiceLinker()
    ffmpeg = _tool("ffmpeg")
    last_step = -10
    try:
        progress("Подготовка аудио для разделения голосов")
        engine = sherpa_onnx.OfflineSpeakerDiarization(config)
        extractor = (
            sherpa_onnx.SpeakerEmbeddingExtractor(embedding_config) if long_recording else None
        )
        with TemporaryDirectory(prefix="ucheba-speakers-") as temporary:
            audio = Path(temporary) / "audio.wav"
            for offset in range(0, int(np.ceil(duration)), chunk_length):
                if cancelled():
                    raise InterruptedError("Разделение голосов отменено.")
                chunk_duration = min(chunk_length, duration - offset)
                _extract_audio(ffmpeg, source, audio, offset, chunk_duration, cancelled)
                samples = _read_pcm(audio)

                def notify(processed: int, total: int) -> int:
                    nonlocal last_step
                    if cancelled():
                        return 1
                    fraction = processed / total if total > 0 else 0
                    percent = min(100, round(100 * (offset + chunk_duration * fraction) / duration))
                    if percent >= last_step + 10:
                        last_step = percent
                        progress(f"Определение говорящих: {percent}%")
                    return 0

                progress("Определение говорящих локальной моделью")
                output = engine.process(samples, notify)
                if cancelled():
                    raise InterruptedError("Разделение голосов отменено.")
                local_turns = [
                    SpeakerTurn(turn.start, turn.end, str(turn.speaker))
                    for turn in output.sort_by_start_time()
                ]
                if extractor is None:
                    turns = local_turns
                    continue
                profiles = _speaker_vectors(extractor, samples, local_turns, cancelled)
                labels = linker.link(local_turns, profiles)
                turns.extend(
                    SpeakerTurn(
                        turn.start + offset, min(turn.end + offset, duration), labels[turn.speaker]
                    )
                    for turn in local_turns
                )
                del samples
    except InterruptedError:
        raise
    except RuntimeError as exc:
        if cancelled():
            raise InterruptedError("Разделение голосов отменено.") from exc
        raise RuntimeError("Встроенная модель не смогла разделить говорящих.") from exc
    return attribute_speakers(segments, turns)
