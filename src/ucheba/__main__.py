import faulthandler
import os
import sys
import traceback
import wave
from pathlib import Path
from tempfile import TemporaryDirectory


def _smoke_test() -> int:
    from faster_whisper import WhisperModel  # noqa: F401

    from ucheba import builtin_diarization
    from ucheba.documents import export
    from ucheba.models import Analysis, Segment
    from ucheba.ui import main as run_ui

    if getattr(sys, "frozen", False):
        import sherpa_onnx  # noqa: F401

    with TemporaryDirectory() as directory:
        if getattr(sys, "frozen", False):
            model_dir = (
                Path(builtin_diarization.__file__).resolve().parent / "models" / "diarization"
            )
            if not (model_dir / "segmentation.onnx").is_file():
                raise RuntimeError("Модель сегментации отсутствует в сборке.")
            if not (model_dir / "embedding-download.txt").is_file():
                audio = Path(directory) / "speaker-smoke.wav"
                with wave.open(str(audio), "wb") as stream:
                    stream.setparams((1, 2, 16_000, 0, "NONE", ""))
                    stream.writeframes(bytes(16_000 * 2))
                builtin_diarization.diarize_builtin(audio, [], model_dir=model_dir)
        export(Path(directory), "Проверка", [Segment(start=0, end=1, text="Лекция")], Analysis())
    return run_ui()


def main() -> int:
    if sys.argv[1:] == ["--smoke-test"]:
        if log_path := os.environ.get("UCHEBA_SMOKE_LOG"):
            with Path(log_path).open("w", encoding="utf-8") as log:
                faulthandler.enable(file=log)
                try:
                    return _smoke_test()
                except Exception:
                    traceback.print_exc(file=log)
                    return 1
                finally:
                    faulthandler.disable()
        return _smoke_test()
    from ucheba.ui import main as run_ui

    return run_ui()


if __name__ == "__main__":
    raise SystemExit(main())
