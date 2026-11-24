"""Stable version metadata for installed and portable Windows builds."""

import re
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

VERSION_PATTERN = r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})"


def current_version() -> str:
    if not getattr(sys, "frozen", False):
        return version("ucheba-ai")
    # An upgrade may leave metadata from older packages in the same directory.
    # The marker is installed as part of this executable's own package.
    marker = Path(__file__).with_name("app-version.txt")
    try:
        with marker.open("r", encoding="ascii") as stream:
            value = stream.read(32).strip()
    except (OSError, UnicodeError):
        raise PackageNotFoundError("ucheba-ai app-version.txt") from None
    if re.fullmatch(VERSION_PATTERN, value) is None:
        raise PackageNotFoundError("ucheba-ai app-version.txt")
    return value
