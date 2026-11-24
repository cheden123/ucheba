"""Build a local faster-whisper overlay that does not require PyAV on import."""

import importlib.metadata
import importlib.util
import shutil
import sys
from pathlib import Path


def create_overlay(overlay: Path) -> None:
    if importlib.metadata.version("faster-whisper") != "1.2.1":
        raise RuntimeError("Обновите патч PyAV для новой версии faster-whisper.")
    spec = importlib.util.find_spec("faster_whisper")
    if spec is None or spec.origin is None:
        raise RuntimeError("Не найден исходный пакет faster-whisper.")
    source = Path(spec.origin).parent
    target = overlay / "faster_whisper"
    shutil.copytree(
        source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__")
    )
    audio = target / "audio.py"
    code = audio.read_text(encoding="utf-8")
    # The app resamples using standalone FFmpeg and passes a NumPy float32 array.
    # Preserve the upstream file-decoding implementation for callers that have PyAV.
    replacements = {
        "\nimport av\n": "\n",
        "    resampler = av.audio.resampler.AudioResampler(": (
            "    import av\n\n    resampler = av.audio.resampler.AudioResampler("
        ),
        "def _ignore_invalid_frames(frames):\n": (
            "def _ignore_invalid_frames(frames):\n    import av\n\n"
        ),
        "def _group_frames(frames, num_samples=None):\n": (
            "def _group_frames(frames, num_samples=None):\n    import av\n\n"
        ),
    }
    for original, patched in replacements.items():
        if code.count(original) != 1:
            raise RuntimeError("Изменился исходник faster-whisper.audio. Проверьте патч сборки.")
        code = code.replace(original, patched)
    compile(code, str(audio), "exec")
    audio.write_text(code, encoding="utf-8")


if __name__ == "__main__":
    create_overlay(Path(sys.argv[1]))
