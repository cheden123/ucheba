import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def main() -> int:
    if sys.argv[1:] == ["--smoke-test"]:
        from faster_whisper import WhisperModel  # noqa: F401

        from ucheba.documents import export
        from ucheba.models import Analysis, Segment

        with TemporaryDirectory() as directory:
            export(
                Path(directory), "Проверка", [Segment(start=0, end=1, text="Лекция")], Analysis()
            )
    from ucheba.ui import main as run_ui

    return run_ui()


if __name__ == "__main__":
    raise SystemExit(main())
