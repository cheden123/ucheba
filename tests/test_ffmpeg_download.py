from pathlib import Path
from zipfile import ZipFile

import pytest

from ucheba import ffmpeg_download as tools


def test_downloaded_tools_reuse_verified_cache(tmp_path: Path, monkeypatch) -> None:
    import hashlib

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    binaries = {"ffmpeg": b"first", "ffprobe": b"second"}
    monkeypatch.setattr(
        tools,
        "_EXECUTABLES",
        {name: hashlib.sha256(data).hexdigest() for name, data in binaries.items()},
    )
    calls = []

    def download(path: Path) -> None:
        calls.append(path)
        with ZipFile(path, "w") as archive:
            for name, data in binaries.items():
                archive.writestr(f"{tools._PACKAGE}/bin/{name}.exe", data)
            archive.writestr(f"{tools._PACKAGE}/LICENSE", "license")

    monkeypatch.setattr(tools, "_download", download)
    ffmpeg = Path(tools.vendor_tool("ffmpeg"))
    ffprobe = Path(tools.vendor_tool("ffprobe"))
    assert ffmpeg.read_bytes() == binaries["ffmpeg"]
    assert ffprobe.read_bytes() == binaries["ffprobe"]
    assert len(calls) == 1
    ffmpeg.write_bytes(b"tampered")
    assert Path(tools.vendor_tool("ffmpeg")).read_bytes() == binaries["ffmpeg"]
    assert len(calls) == 2


def test_unknown_tool_is_not_downloaded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    with pytest.raises(tools.FFmpegDownloadError):
        tools.vendor_tool("unknown")
    assert not list(tmp_path.iterdir())
