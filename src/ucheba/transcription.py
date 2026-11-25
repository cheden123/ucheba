"""Локальная расшифровка с ограниченным размером аудиофрагмента."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

import numpy as np
from numpy.typing import NDArray

from .models import Segment

_CHUNK_SECONDS = 300
_OVERLAP_SECONDS = 8
_CHECKPOINT_VERSION = 1
_CANCELLED = "Расшифровка отменена. Готовые фрагменты сохранены."


class TranscriptionError(RuntimeError):
    """Сообщение о расшифровке, пригодное для показа пользователю."""


def _continue(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise InterruptedError(_CANCELLED)


def _tool(name: str) -> str:
    bundled = Path(sys.executable).parent / f"{name}.exe"
    if getattr(sys, "frozen", False) and (bundled.parent / "download-ffmpeg.txt").is_file():
        from .ffmpeg_download import FFmpegDownloadError, vendor_tool

        try:
            return vendor_tool(name)
        except FFmpegDownloadError as exc:
            raise TranscriptionError(str(exc)) from exc
    if getattr(sys, "frozen", False) and bundled.is_file():
        return str(bundled)
    result = shutil.which(name)
    if result is None:
        raise TranscriptionError(f"Не найден {name}. Установите FFmpeg и добавьте его в PATH.")
    return result


def _run(
    command: list[str],
    cancelled: Callable[[], bool],
    timeout: float,
    *,
    stdin: BinaryIO | None = None,
) -> str:
    _continue(cancelled)
    try:
        with subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL if stdin is None else stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ) as process:
            deadline = time.monotonic() + timeout
            try:
                while True:
                    _continue(cancelled)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TranscriptionError("Истекло время ожидания FFmpeg или FFprobe.")
                    try:
                        output, _ = process.communicate(timeout=min(remaining, 0.5))
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                if process.poll() is None:
                    process.kill()
                process.communicate()
                raise
            if process.returncode:
                raise TranscriptionError(
                    "FFmpeg или FFprobe не смог обработать запись. "
                    "Проверьте формат, доступность файла и свободное место."
                )
    except (FileNotFoundError, PermissionError) as exc:
        raise TranscriptionError("Не удалось запустить FFmpeg или FFprobe.") from exc
    return output.decode("utf-8", errors="replace")


def _duration(source: Path, cancelled: Callable[[], bool]) -> float:
    output = _run(
        [
            _tool("ffprobe"),
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index:format=duration",
            "-of",
            "json",
            str(source),
        ],
        cancelled,
        60,
    )
    try:
        probe = json.loads(output)
        if not probe.get("streams"):
            raise TranscriptionError("В исходной записи отсутствует аудиодорожка.")
        duration = float(probe["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError
    except (ValueError, TypeError, KeyError) as exc:
        raise TranscriptionError("Не удалось определить длительность записи.") from exc
    return duration


def _extract_audio(
    ffmpeg: str,
    source: Path,
    audio: Path,
    start: float,
    duration: float,
    cancelled: Callable[[], bool],
) -> None:
    _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-protocol_whitelist",
            "file",
            "-ss",
            str(start),
            "-i",
            str(source),
            "-t",
            str(duration),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(audio),
        ],
        cancelled,
        600,
    )


def _read_pcm(audio: Path) -> NDArray[np.float32]:
    with wave.open(str(audio), "rb") as stream:
        if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) != (1, 2, 16_000):
            raise TranscriptionError("Не удалось подготовить монофонический звук для Whisper.")
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(
            np.float32
        )
    samples /= 32_768
    return samples


def _load_checkpoint(
    path: Path, signature: dict[str, object], chunks: int
) -> tuple[int, list[Segment]]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0, []
    except ValueError as exc:
        raise TranscriptionError("Контрольная точка расшифровки повреждена.") from exc
    if not isinstance(state, dict) or state.get("signature") != signature:
        return 0, []
    try:
        next_chunk = state["next_chunk"]
        if type(next_chunk) is not int or not 0 <= next_chunk <= chunks:
            raise ValueError
        return next_chunk, [Segment(**row) for row in state["segments"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise TranscriptionError("Контрольная точка расшифровки повреждена.") from exc


def _save_checkpoint(
    path: Path, signature: dict[str, object], next_chunk: int, segments: list[Segment]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    )
    pending = Path(stream.name)
    try:
        with stream:
            json.dump(
                {
                    "signature": signature,
                    "next_chunk": next_chunk,
                    "segments": [
                        {
                            "start": segment.start,
                            "end": segment.end,
                            "text": segment.text,
                        }
                        for segment in segments
                    ],
                },
                stream,
                ensure_ascii=False,
                allow_nan=False,
            )
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def transcribe(
    source: Path,
    checkpoint: Path,
    model: str = "small",
    device: str = "cpu",
    language: str | None = "ru",
    progress: Callable[[str], None] = lambda message: None,
    cancelled: Callable[[], bool] = lambda: False,
    *,
    glossary: str = "",
    stage: Callable[[str], None] = lambda _stage: None,
) -> list[Segment]:
    """Распознать запись, сохраняя результат после каждого пятиминутного интервала."""
    stage("probe_media")
    _continue(cancelled)
    source = source.resolve()
    if not source.is_file():
        raise TranscriptionError("Исходная запись недоступна.")
    if source == checkpoint.resolve():
        raise TranscriptionError("Файл контрольной точки совпадает с исходной записью.")
    if device not in {"cpu", "cuda", "auto"}:
        raise TranscriptionError("Выберите режим распознавания CPU, CUDA или авто.")
    source_stat = source.stat()
    duration = _duration(source, cancelled)
    chunks = math.ceil(duration / _CHUNK_SECONDS)
    signature = {
        "version": _CHECKPOINT_VERSION,
        "source": str(source),
        "size": source_stat.st_size,
        "modified": source_stat.st_mtime_ns,
        "duration": duration,
        "model": model,
        "device": device,
        "language": language,
        "glossary": glossary,
    }
    first_chunk, result = _load_checkpoint(checkpoint, signature, chunks)
    if first_chunk == chunks:
        stage("transcribe")
        return result
    ffmpeg = _tool("ffmpeg")
    stage("load_whisper")
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscriptionError("Нужна исправная установка faster-whisper и CTranslate2.") from exc
    _continue(cancelled)
    progress(f"Подготовка модели Whisper «{model}». При первом запуске ей нужна загрузка.")
    alias = "turbo" if model == "large-v3-turbo" else model
    try:
        recognizer = WhisperModel(
            alias, device=device, compute_type="int8" if device == "cpu" else "auto"
        )
    except ImportError as exc:
        raise TranscriptionError(
            "Не удалось загрузить библиотеку Whisper. Переустановите приложение из новой сборки."
        ) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise TranscriptionError(
            "Не удалось загрузить модель Whisper. Проверьте подключение, место на диске "
            "и попробуйте обработку на CPU."
        ) from exc
    with tempfile.TemporaryDirectory(prefix="ucheba-audio-") as temp:
        audio = Path(temp) / "audio.wav"
        for chunk in range(first_chunk, chunks):
            _continue(cancelled)
            progress(f"Распознавание фрагмента {chunk + 1} из {chunks}")
            start = chunk * _CHUNK_SECONDS
            stop = min(start + _CHUNK_SECONDS, duration)
            extract_start = max(0, start - _OVERLAP_SECONDS)
            extract_stop = min(duration, stop + _OVERLAP_SECONDS)
            stage("convert")
            _extract_audio(
                ffmpeg, source, audio, extract_start, extract_stop - extract_start, cancelled
            )
            stage("transcribe")
            try:
                samples = _read_pcm(audio)
                if samples.size:
                    parts, _ = recognizer.transcribe(
                        samples,
                        language=language,
                        vad_filter=True,
                        initial_prompt=glossary or None,
                    )
                    for part in parts:
                        _continue(cancelled)
                        absolute_start = max(0.0, part.start + extract_start)
                        absolute_end = min(duration, part.end + extract_start)
                        middle = (absolute_start + absolute_end) / 2
                        text = part.text.strip()
                        if text and absolute_end > absolute_start and start <= middle < stop:
                            result.append(
                                Segment(start=absolute_start, end=absolute_end, text=text)
                            )
            except (InterruptedError, TranscriptionError):
                raise
            except (OSError, RuntimeError, TypeError, ValueError, EOFError, wave.Error) as exc:
                raise TranscriptionError(
                    "Модель не смогла распознать фрагмент. "
                    "Если используется CUDA, попробуйте режим CPU."
                ) from exc
            status = source.stat()
            if (status.st_size, status.st_mtime_ns) != (
                source_stat.st_size,
                source_stat.st_mtime_ns,
            ):
                raise TranscriptionError("Исходная запись изменилась во время распознавания.")
            _save_checkpoint(checkpoint, signature, chunk + 1, result)
    return result
