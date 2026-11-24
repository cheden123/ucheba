import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from ucheba import slides


def test_unique_slide_times_and_blank_frame_rejection(tmp_path):
    first = np.full((48, 80), 250, dtype=np.uint8)
    first[4:28, 4:40] = 5
    second = np.full((48, 80), 250, dtype=np.uint8)
    second[24:42, 32:70] = 5
    blank = np.zeros((48, 80), dtype=np.uint8)
    frames = tmp_path / "thumbnails"
    frames.write_bytes(b"".join(item.tobytes() for item in [blank, first, first, second, first]))
    assert list(slides._candidate_times(frames, 30, 6, lambda: False)) == [6.0, 18.0]


def test_cancelled_slide_extraction_does_not_touch_output(tmp_path):
    output = tmp_path / "frames"
    with pytest.raises(InterruptedError, match="слайдов отменено"):
        slides.extract_slides(tmp_path / "source.mp4", output, cancelled=lambda: True)
    assert not output.exists()


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Для проверки видеоконвейера нужны FFmpeg и FFprobe в PATH",
)
def test_extract_slides_with_real_ffmpeg_and_local_ocr_command(monkeypatch, tmp_path):
    movie = tmp_path / "Лекция со слайдами.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=320x240:r=4:d=24",
            "-vf",
            "drawbox=x=10:y=10:w=180:h=100:color=black:t=fill:enable='lt(t,8)+gte(t,16)',"
            "drawbox=x=110:y=110:w=170:h=100:color=black:t=fill:enable='between(t,8,16)'",
            "-c:v",
            "libx264",
            "-an",
            str(movie),
        ],
        check=True,
    )
    ocr = tmp_path / "tesseract.exe"
    ocr.touch()
    run = slides._run

    def execute(command, cancelled, timeout, *, stdin=None):
        if command[0] == str(ocr):
            assert Path(stdin.name).is_file()
            assert stdin.read(4) == b"\x89PNG"
            assert command[1:] == ["stdin", "stdout", "-l", "rus+eng", "--psm", "11"]
            return "Текст со слайда\n"
        return run(command, cancelled, timeout)

    monkeypatch.setattr(slides, "_run", execute)
    output = tmp_path / "Кадры"
    result = slides.extract_slides(movie, output, tesseract=ocr)
    assert [item.start for item in result] == [0.0, 12.0]
    assert all(item.image.read_bytes().startswith(b"\x89PNG") for item in result)
    assert all(item.image.parent == output and item.text == "Текст со слайда" for item in result)
    with pytest.raises(slides.SlideError, match="уже существует"):
        slides.extract_slides(movie, output)
