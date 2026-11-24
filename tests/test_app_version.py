from pathlib import Path

import pytest

from ucheba import app_version


def test_frozen_version_ignores_stale_distribution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_version.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_version, "__file__", str(tmp_path / "app_version.py"))
    monkeypatch.setattr(app_version, "version", lambda _: "0.3.0")
    marker = tmp_path / "app-version.txt"
    marker.write_text("0.4.0\n", encoding="ascii")
    assert app_version.current_version() == "0.4.0"
    marker.write_text("0.4.0\ncorrupt", encoding="ascii")
    with pytest.raises(app_version.PackageNotFoundError):
        app_version.current_version()
