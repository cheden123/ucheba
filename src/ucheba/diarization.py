"""Необязательное локальное разделение голосов через pyannote Community-1."""

from __future__ import annotations

import os
import re
import tempfile
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import Segment
from .transcription import _duration, _extract_audio, _tool

_MAX_DURATION = 3600
_LECTURE_MARKERS = re.compile(
    r"\b(?:рассмотрим|разбер[её]м|запишите|запомните|откройте|обратите внимание|"
    r"выполните|сдайте|переходим к|домашнее задание|на прошлой лекции|"
    r"на следующем занятии|привед[её]м пример)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SpeakerTurn:
    start: float
    end: float
    speaker: str


@dataclass(frozen=True)
class DiarizationResult:
    segments: list[Segment]
    turns: list[SpeakerTurn]
    candidate: str | None


def attribute_speakers(
    segments: Sequence[Segment], turns: Sequence[SpeakerTurn]
) -> DiarizationResult:
    ordered = sorted(turns, key=lambda turn: (turn.start, turn.end, turn.speaker))
    labels: dict[str, str] = {}
    labeled_turns = []
    for turn in ordered:
        if turn.end <= turn.start:
            continue
        label = labels.setdefault(turn.speaker, f"Говорящий {len(labels) + 1}")
        labeled_turns.append(SpeakerTurn(turn.start, turn.end, label))
    result = list(segments)
    first = 0
    for index, segment in sorted(enumerate(segments), key=lambda row: row[1].start):
        if segment.speaker is not None or segment.end <= segment.start:
            continue
        while first < len(labeled_turns) and labeled_turns[first].end <= segment.start:
            first += 1
        overlaps: dict[str, float] = defaultdict(float)
        for turn_index in range(first, len(labeled_turns)):
            turn = labeled_turns[turn_index]
            if turn.start >= segment.end:
                break
            overlap = min(segment.end, turn.end) - max(segment.start, turn.start)
            if overlap > 0:
                overlaps[turn.speaker] += overlap
        ordered_overlap = sorted(overlaps.items(), key=lambda row: row[1], reverse=True)
        if not ordered_overlap:
            continue
        speaker, best = ordered_overlap[0]
        runner_up = ordered_overlap[1][1] if len(ordered_overlap) > 1 else 0.0
        length = segment.end - segment.start
        if best >= 0.55 * length and best - runner_up >= 0.15 * length:
            result[index] = Segment(
                start=segment.start, end=segment.end, text=segment.text, speaker=speaker
            )
    return DiarizationResult(result, labeled_turns, suggest_lecturer(result))


def suggest_lecturer(segments: Sequence[Segment]) -> str | None:
    speech: dict[str, float] = defaultdict(float)
    cues: dict[str, int] = defaultdict(int)
    for segment in segments:
        if segment.speaker and segment.end > segment.start:
            speech[segment.speaker] += segment.end - segment.start
            if not segment.text.rstrip().endswith("?") and _LECTURE_MARKERS.search(segment.text):
                cues[segment.speaker] += 1
    ranked = sorted(
        ((count, speech[speaker], speaker) for speaker, count in cues.items()),
        reverse=True,
    )
    if not ranked:
        return None
    count, duration, speaker = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0
    if count < 2 or duration < 30 or count - second < 2:
        return None
    return speaker


def diarize(
    source: Path,
    segments: Sequence[Segment],
    model_path: Path,
    *,
    progress: Callable[[str], None] = lambda message: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> DiarizationResult:
    """Сопоставить голоса с расшифровкой. Кандидат на роль преподавателя требует подтверждения."""

    def check_cancelled() -> None:
        if cancelled():
            raise InterruptedError("Разделение голосов отменено.")

    check_cancelled()
    model_path = model_path.resolve()
    if not (model_path / "config.yaml").is_file():
        raise ValueError("Укажите папку скачанной модели pyannote Community-1 с config.yaml.")
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError("Исходная запись недоступна.")
    duration = _duration(source, cancelled)
    if duration > _MAX_DURATION:
        raise ValueError("Разделение голосов доступно для записей длительностью до одного часа.")
    os.environ["PYANNOTE_METRICS_ENABLED"] = "0"
    try:
        from pyannote.audio import Pipeline
    except (ImportError, OSError, RuntimeError) as exc:
        raise RuntimeError("Нужно установить совместимое окружение pyannote.audio 4.") from exc

    def step(_name: str, _result: object, **_kwargs: object) -> None:
        check_cancelled()

    try:
        progress("Загрузка локальной модели разделения голосов")
        pipeline = Pipeline.from_pretrained(str(model_path))
        check_cancelled()
        with tempfile.TemporaryDirectory(prefix="ucheba-speakers-") as temp:
            audio = Path(temp) / "audio.wav"
            _extract_audio(_tool("ffmpeg"), source, audio, 0, duration, cancelled)
            progress("Определение говорящих")
            output = pipeline(str(audio), hook=step)
            check_cancelled()
            turns = [
                SpeakerTurn(region.start, region.end, str(label))
                for region, _, label in output.exclusive_speaker_diarization.itertracks(
                    yield_label=True
                )
            ]
    except InterruptedError:
        raise
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "Не удалось разделить голоса. Проверьте комплектность локальной модели "
            "Community-1 и доступность FFmpeg."
        ) from exc
    return attribute_speakers(segments, turns)
