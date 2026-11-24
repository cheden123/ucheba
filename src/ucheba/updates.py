"""Проверка обновлений из GitHub и локальных папок выпуска."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from tempfile import mkdtemp
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


class NoPublicRelease(UpdateError):
    """GitHub не предоставил публичный выпуск."""


@dataclass(frozen=True)
class Update:
    version: str
    notes: str
    release_url: str
    installer_size: int


@dataclass(frozen=True)
class VerifiedInstaller:
    path: Path
    version: str
    sha256: str
    size: int
    signed: bool


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
    current: str | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    require_public: bool = False,
) -> Update | None:
    """Получить сведения о новом стабильном выпуске."""
    local = _version_parts(current if current is not None else version("ucheba-ai"))
    try:
        with _client(transport) as client:
            response = client.get(_LATEST, headers={"Accept": "application/vnd.github+json"})
            if response.status_code == 404:
                if require_public:
                    raise NoPublicRelease("GitHub не предоставил общедоступный стабильный выпуск.")
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


def _read_windows_installer(path: Path, command: str) -> str:
    if sys.platform != "win32":
        raise UpdateError("Проверка установщика доступна в Windows.")
    shell = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    shell /= "System32/WindowsPowerShell/v1.0/powershell.exe"
    environment = dict(os.environ)
    environment.pop("PSMODULEPATH", None)
    environment["UCHEBA_UPDATE_FILE"] = str(path)
    try:
        result = subprocess.run(
            [str(shell), "-NoProfile", "-NonInteractive", "-Command", command],
            env=environment,
            capture_output=True,
            text=True,
            timeout=45,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise UpdateError("Не удалось прочитать данные установщика Windows.") from None
    return result.stdout.strip()


def _verify_signature(path: Path) -> bool:
    status = _read_windows_installer(
        path, "(Get-AuthenticodeSignature -LiteralPath $env:UCHEBA_UPDATE_FILE).Status"
    )
    if status not in {"Valid", "NotSigned"}:
        raise UpdateError("Цифровая подпись установщика недействительна.")
    return status == "Valid"


def _installer_version(path: Path) -> str:
    product = _read_windows_installer(
        path,
        "$info = (Get-Item -LiteralPath $env:UCHEBA_UPDATE_FILE).VersionInfo; "
        "if (!$info.ProductName -or $info.ProductName.Trim() -cne 'Учёба AI') { exit 1 }; "
        "$info.ProductVersion",
    )
    _version_parts(product)
    return product


def _installer_hash(manifest: bytes) -> str:
    if len(manifest) > _MAX_MANIFEST:
        raise UpdateError("Файл контрольных сумм слишком большой.")
    hashes = re.findall(
        rb"(?m)^([a-fA-F0-9]{64}) [ *]UchebaAI-Setup-x64\.exe\r?$",
        manifest.removeprefix(b"\xef\xbb\xbf"),
    )
    if len(hashes) != 1:
        raise UpdateError("Установщик отсутствует в списке SHA-256 или указан несколько раз.")
    return hashes[0].decode("ascii").lower()


def _stage_installer(
    chunks: Iterable[bytes],
    directory: Path,
    size: int,
    checksum: str,
    *,
    expected_version: str | None = None,
    current: str | None = None,
) -> VerifiedInstaller:
    if not 0 < size <= _MAX_INSTALLER:
        raise UpdateError("Недопустимый размер установщика.")
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(mkdtemp(prefix="ucheba-update-", dir=directory))
    destination = temporary_dir / _SETUP
    try:
        digest = hashlib.sha256()
        written = 0
        with destination.open("xb") as stream:
            for chunk in chunks:
                written += len(chunk)
                if written > size:
                    raise UpdateError("Размер установщика превышает ожидаемый.")
                digest.update(chunk)
                stream.write(chunk)
        if written != size or digest.hexdigest() != checksum:
            raise UpdateError("Размер или контрольная сумма установщика не совпадает.")
        installed_version = expected_version
        if installed_version is None:
            installed_version = _installer_version(destination)
            local = current if current is not None else version("ucheba-ai")
            if _version_parts(installed_version) <= _version_parts(local):
                raise UpdateError("Версия установщика должна быть новее установленной.")
        signed = _verify_signature(destination)
        return VerifiedInstaller(destination, installed_version, checksum, size, signed)
    except Exception:
        destination.unlink(missing_ok=True)
        temporary_dir.rmdir()
        raise


def stage_online_update(
    update: Update,
    directory: Path,
    *,
    transport: httpx.BaseTransport | None = None,
) -> VerifiedInstaller:
    """Загрузить установщик в отдельную папку профиля Windows."""
    _version_parts(update.version)
    base = f"{_DOWNLOAD}/v{update.version}"
    try:
        with _client(transport) as client:
            manifest = b"".join(_asset_bytes(client, f"{base}/{_CHECKSUM}", _MAX_MANIFEST))
            return _stage_installer(
                _asset_bytes(client, f"{base}/{_SETUP}", update.installer_size),
                directory,
                update.installer_size,
                _installer_hash(manifest),
                expected_version=update.version,
            )
    except (OSError, httpx.HTTPError, KeyError, ValueError):
        raise UpdateError("Не удалось загрузить и сохранить установщик обновления.") from None


def download_update(
    update: Update,
    directory: Path,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    """Загрузить и проверить установщик GitHub."""
    return stage_online_update(update, directory, transport=transport).path


def stage_offline_update(
    folder: Path, directory: Path, current: str | None = None
) -> VerifiedInstaller:
    """Проверить установщик и SHA256SUMS.txt из доверенной папки выпуска."""
    source = folder / _SETUP
    try:
        files = (source, folder / _CHECKSUM)
        if not all(item.is_file() and not item.is_symlink() for item in files):
            raise UpdateError(f"В папке выпуска должны находиться {_SETUP} и {_CHECKSUM}.")
        with files[1].open("rb") as manifest:
            checksum = _installer_hash(manifest.read(_MAX_MANIFEST + 1))
        with source.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            return _stage_installer(
                iter(lambda: stream.read(128 * 1024), b""),
                directory,
                size,
                checksum,
                current=current,
            )
    except OSError:
        raise UpdateError("Не удалось прочитать или сохранить локальный установщик.") from None


def verify_installer_for_launch(installer: VerifiedInstaller) -> Path:
    """Повторить проверку сохранённого файла перед запуском установщика."""
    path = installer.path
    try:
        if path.is_symlink() or not path.is_file():
            raise UpdateError("Подготовленный установщик отсутствует или был заменён.")
        with path.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size != installer.size:
                raise UpdateError("Размер подготовленного установщика изменился.")
            if hashlib.file_digest(stream, "sha256").hexdigest() != installer.sha256:
                raise UpdateError("Контрольная сумма подготовленного установщика изменилась.")
        if _verify_signature(path) != installer.signed:
            raise UpdateError("Состояние цифровой подписи установщика изменилось.")
    except OSError:
        raise UpdateError("Не удалось повторно проверить подготовленный установщик.") from None
    return path
