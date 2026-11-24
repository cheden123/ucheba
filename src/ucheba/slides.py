"""Отбор уникальных кадров лекции и необязательное локальное OCR."""

from __future__ import annotations

import json
import math
import re
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from .transcription import TranscriptionError, _run, _tool

_WIDTH = 80
_HEIGHT = 48
_MAX_SAMPLES = 4_800
_MAX_SLIDES = 180


class SlideError(RuntimeError):
    """Сообщение об извлечении слайдов, пригодное для показа пользователю."""


@dataclass(frozen=True)
class Slide:
    start: float
    image: Path
    text: str


@dataclass(frozen=True)
class _Fingerprint:
    bits: int
    pixels: NDArray[np.int16]


def _check_cancelled(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise InterruptedError("Извлечение слайдов отменено.")


def _video_duration(source: Path, cancelled: Callable[[], bool]) -> float | None:
    metadata = json.loads(
        _run(
            [
                _tool("ffprobe"),
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-select_streams",
                "V:0",
                "-show_entries",
                "stream=duration,index:format=duration",
                "-of",
                "json",
                str(source),
            ],
            cancelled,
            60,
        )
    )
    streams = metadata.get("streams", [])
    if not streams:
        return None
    durations = (streams[0].get("duration"), metadata.get("format", {}).get("duration"))
    for value in durations:
        try:
            duration = float(value)
        except (ValueError, TypeError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    raise SlideError("Не удалось определить длительность видеодорожки.")


def _fingerprint(raw: bytes) -> _Fingerprint | None:
    pixels = np.frombuffer(raw, dtype=np.uint8).reshape(_HEIGHT, _WIDTH).astype(np.int16)
    if pixels.std() < 6:
        return None
    edge_strength = np.abs(np.diff(pixels, axis=0)).mean() + np.abs(np.diff(pixels, axis=1)).mean()
    if edge_strength < 1.5:
        return None
    reduced = pixels[:, 4:-4].reshape(8, 6, 9, 8).mean(axis=(1, 3))
    bits = np.packbits((reduced[:, 1:] > reduced[:, :-1]).ravel())
    return _Fingerprint(int.from_bytes(bits.tobytes(), "big"), pixels)


def _duplicate(current: _Fingerprint, previous: _Fingerprint) -> bool:
    return (current.bits ^ previous.bits).bit_count() <= 12 and bool(
        np.abs(current.pixels - previous.pixels).mean() <= 9
    )


def _candidate_times(
    thumbnails: Path,
    duration: float,
    step: int,
    cancelled: Callable[[], bool],
) -> Iterator[float]:
    accepted: list[_Fingerprint] = []
    sample_size = _WIDTH * _HEIGHT
    with thumbnails.open("rb") as stream:
        for number in range(_MAX_SAMPLES):
            _check_cancelled(cancelled)
            raw = stream.read(sample_size)
            start = float(number * step)
            if len(raw) < sample_size or start >= duration:
                return
            current = _fingerprint(raw)
            if current is None or any(_duplicate(current, previous) for previous in accepted):
                continue
            accepted.append(current)
            yield start
            if len(accepted) == _MAX_SLIDES:
                return


def extract_slides(
    source: Path,
    output: Path,
    *,
    progress: Callable[[str], None] = lambda message: None,
    cancelled: Callable[[], bool] = lambda: False,
    tesseract: Path | None = None,
    ocr_language: str = "rus+eng",
) -> list[Slide]:
    """Сохранить до 180 уникальных кадров и распознать текст при наличии Tesseract."""
    _check_cancelled(cancelled)
    source = source.resolve()
    if not source.is_file():
        raise SlideError("Исходная видеозапись недоступна.")
    output = output.resolve()
    if output.exists():
        raise SlideError("Папка извлечённых кадров уже существует.")
    if tesseract is not None:
        tesseract = tesseract.absolute()
        if not tesseract.is_file() or not re.fullmatch(r"[a-z]{3}(?:\+[a-z]{3})*", ocr_language):
            raise SlideError("Проверьте путь к Tesseract и языки распознавания.")
    try:
        duration = _video_duration(source, cancelled)
        if duration is None:
            return []
        ffmpeg = _tool("ffmpeg")
        step = max(6, math.ceil(duration / _MAX_SAMPLES))
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".ucheba-slides-", dir=output.parent) as scratch:
            scratch_path = Path(scratch)
            thumbnails = scratch_path / "sample.gray"
            frames = scratch_path / "frames"
            frames.mkdir()
            progress("Поиск смены слайдов в записи")
            _run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-v",
                    "error",
                    "-nostdin",
                    "-protocol_whitelist",
                    "file",
                    "-i",
                    str(source),
                    "-map",
                    "0:V:0",
                    "-an",
                    "-vf",
                    f"fps=1/{step}:start_time=0:round=up:eof_action=pass,"
                    f"scale={_WIDTH}:{_HEIGHT}:flags=area,format=gray",
                    "-frames:v",
                    str(_MAX_SAMPLES),
                    "-pix_fmt",
                    "gray",
                    "-threads:v",
                    "1",
                    "-f",
                    "rawvideo",
                    str(thumbnails),
                ],
                cancelled,
                max(120, min(32_400, duration + 120)),
            )
            slides: list[Slide] = []
            for index, start in enumerate(
                _candidate_times(thumbnails, duration, step, cancelled), 1
            ):
                progress(f"Сохранение кадра {index}")
                name = f"slide-{index:03d}.png"
                image = frames / name
                _run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-v",
                        "error",
                        "-nostdin",
                        "-protocol_whitelist",
                        "file",
                        "-ss",
                        str(start),
                        "-i",
                        str(source),
                        "-map",
                        "0:V:0",
                        "-an",
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=w='min(iw,1600)':h='min(ih,1200)':"
                        "force_original_aspect_ratio=decrease:"
                        "force_divisible_by=2",
                        "-update",
                        "1",
                        str(image),
                    ],
                    cancelled,
                    60,
                )
                if not image.is_file():
                    continue
                text = ""
                if tesseract is not None:
                    try:
                        with image.open("rb") as data:
                            text = _run(
                                [
                                    str(tesseract),
                                    "stdin",
                                    "stdout",
                                    "-l",
                                    ocr_language,
                                    "--psm",
                                    "11",
                                ],
                                cancelled,
                                120,
                                stdin=data,
                            ).strip()
                    except TranscriptionError:
                        progress(
                            "Tesseract недоступен. Слайды сохраняются без распознавания текста."
                        )
                        tesseract = None
                slides.append(Slide(start, output / name, text))
            _check_cancelled(cancelled)
            if slides:
                frames.rename(output)
            if len(slides) == _MAX_SLIDES:
                progress(f"Сохранено максимально допустимое число слайдов: {_MAX_SLIDES}.")
            return slides
    except InterruptedError:
        raise
    except (OSError, TranscriptionError, ValueError, TypeError) as exc:
        raise SlideError("Не удалось обработать видеодорожку и сохранить слайды.") from exc
