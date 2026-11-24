import os
from pathlib import Path

import pytest

from ucheba import hardware


@pytest.mark.skipif(os.name != "nt", reason="Windows CUDA DLL loading")
def test_cuda_preflight_does_not_reject_configured_path(tmp_path: Path, monkeypatch) -> None:
    for library in ("cublas64_12.dll", "cudnn64_9.dll"):
        (tmp_path / library).touch()
    monkeypatch.setenv("PATH", str(tmp_path))

    def does_not_use_path(_name: str) -> None:
        raise OSError("WinDLL safe search did not locate the file")

    monkeypatch.setattr(hardware.ctypes, "WinDLL", does_not_use_path)
    assert hardware.missing_cuda_libraries() == ()
