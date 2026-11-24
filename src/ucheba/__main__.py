import faulthandler
import os
import sys
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory


def _smoke_test() -> int:
    from faster_whisper import WhisperModel  # noqa: F401

    from ucheba.documents import export
    from ucheba.models import Analysis, Segment
    from ucheba.ui import main as run_ui

    with TemporaryDirectory() as directory:
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
