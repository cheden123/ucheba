"""Проверка обновлений через публичные выпуски GitHub."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable

import httpx

_REPOSITORY = "cheden123/ucheba"
_LATEST = f"https://api.github.com/repos/{_REPOSITORY}/releases/latest"
_DOWNLOAD = f"https://github.com/{_REPOSITORY}/releases/download"
_SETUP = "UchebaAI-Setup-x64.exe"
_CHECKSUM = "SHA256SUMS.txt"
_VERSION = r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})"
_ASSET_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}
_MAX_INSTALLER = 1024**3
_MAX_MANIFEST = 64 * 1024


class UpdateError(RuntimeError):
    """Обновление нельзя безопасно получить."""


@dataclass(frozen=True)
class Update:
    version: str
    notes: str
    release_url: str
    installer_size: int


def _version_parts(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(_VERSION, value)
    if match is None:
        raise UpdateError("Обнаружен выпуск с неподдерживаемым форматом версии.")
    return int(match[1]), int(match[2]), int(match[3])


def _client(transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(30.0, connect=10.0),
        headers={"User-Agent": "UchebaAI"},
        follow_redirects=False,
    )


def check_update(
    current: str | None = None, *, transport: httpx.BaseTransport | None = None
) -> Update | None:
    """Получить сведения о новом стабильном выпуске."""
    local = _version_parts(current if current is not None else version("ucheba-ai"))
    try:
        with _client(transport) as client:
            response = client.get(_LATEST, headers={"Accept": "application/vnd.github+json"})
            if response.status_code == 404:
                return None
            response.raise_for_status()
            release = response.json()
    except (httpx.HTTPError, ValueError):
        raise UpdateError("Не удалось проверить обновления через GitHub.") from None
    if not isinstance(release, dict):
        raise UpdateError("GitHub вернул неизвестный формат выпуска.")
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise UpdateError("У выпуска отсутствует корректный тег версии.")
    if release.get("draft") is not False or release.get("prerelease") is not False:
        return None
    if _version_parts(tag[1:]) <= local:
        return None
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("GitHub не вернул список файлов выпуска.")
    sizes: dict[str, int] = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            continue
        name = asset["name"]
        if name not in (_SETUP, _CHECKSUM):
            continue
        size = asset.get("size")
        if (
            type(size) is not int
            or not 0 < size <= (_MAX_INSTALLER if name == _SETUP else _MAX_MANIFEST)
            or name in sizes
            or asset.get("browser_download_url") != f"{_DOWNLOAD}/{tag}/{name}"
        ):
            raise UpdateError("Файл выпуска не прошёл проверку размера или адреса.")
        sizes[name] = size
    if sizes.keys() != {_SETUP, _CHECKSUM}:
        raise UpdateError("В выпуске отсутствует установщик или файл контрольных сумм.")
    notes = release.get("body")
    return Update(
        version=tag[1:],
        notes=notes[:25_000] if isinstance(notes, str) else "",
        release_url=f"https://github.com/{_REPOSITORY}/releases/tag/{tag}",
        installer_size=sizes[_SETUP],
    )


def _asset_bytes(client: httpx.Client, target: str, limit: int) -> Iterable[bytes]:
    url = httpx.URL(target)
    for _ in range(5):
        if (
            url.scheme != "https"
            or url.host not in _ASSET_HOSTS
            or url.port not in (None, 443)
            or url.userinfo
        ):
            raise UpdateError("GitHub перенаправил загрузку на неизвестный адрес.")
        with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
            if response.is_redirect:
                url = url.join(response.headers["location"])
                continue
            response.raise_for_status()
            size = 0
            for chunk in response.iter_bytes(1024 * 128):
                size += len(chunk)
                if size > limit:
                    raise UpdateError("Размер загружаемого файла превышает допустимый.")
                yield chunk
            return
    raise UpdateError("GitHub вернул слишком много перенаправлений.")


def _verify_signature(path: Path) -> None:
    if sys.platform != "win32":
        return
    shell = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    shell /= "System32/WindowsPowerShell/v1.0/powershell.exe"
    environment = dict(os.environ)
    environment.pop("PSMODULEPATH", None)
    environment["UCHEBA_UPDATE_FILE"] = str(path)
    try:
        result = subprocess.run(
            [
                str(shell),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-AuthenticodeSignature -LiteralPath $env:UCHEBA_UPDATE_FILE).Status",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=45,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        raise UpdateError("Не удалось выполнить проверку цифровой подписи установщика.") from None
    if result.stdout.strip() not in {"Valid", "NotSigned"}:
        raise UpdateError("Цифровая подпись установщика недействительна.")


def download_update(
    update: Update,
    directory: Path,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    """Сохранить установщик после проверки GitHub SHA-256 и Authenticode."""
    _version_parts(update.version)
    if not 0 < update.installer_size <= _MAX_INSTALLER:
        raise UpdateError("Недопустимый размер установщика.")
    base = f"{_DOWNLOAD}/v{update.version}"
    temporary: Path | None = None
    try:
        with _client(transport) as client:
            manifest = b"".join(_asset_bytes(client, f"{base}/{_CHECKSUM}", _MAX_MANIFEST))
            hashes = re.findall(
                rb"(?m)^([a-fA-F0-9]{64}) [ *]UchebaAI-Setup-x64\.exe\r?$", manifest
            )
            if len(hashes) != 1:
                raise UpdateError(
                    "Установщик отсутствует в списке SHA-256 или указан несколько раз."
                )
            directory = directory.resolve()
            directory.mkdir(parents=True, exist_ok=True)
            size = 0
            digest = hashlib.sha256()
            with NamedTemporaryFile(dir=directory, suffix=".exe", delete=False) as stream:
                temporary = Path(stream.name)
                for chunk in _asset_bytes(client, f"{base}/{_SETUP}", update.installer_size):
                    size += len(chunk)
                    digest.update(chunk)
                    stream.write(chunk)
            if size != update.installer_size or digest.hexdigest() != hashes[0].decode().lower():
                raise UpdateError("Размер или контрольная сумма установщика не совпадает.")
            _verify_signature(temporary)
            destination = directory / f"UchebaAI-Setup-x64-v{update.version}.exe"
            temporary.replace(destination)
            return destination
    except (OSError, httpx.HTTPError, KeyError, ValueError):
        raise UpdateError("Не удалось загрузить и сохранить установщик обновления.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
