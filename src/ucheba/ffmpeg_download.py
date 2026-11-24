"""First-use installation of the pinned upstream FFmpeg tools on Windows."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from zipfile import ZipFile

import httpx

_PACKAGE = "ffmpeg-9.1-essentials_build"
_URL = f"https://github.com/GyanD/codexffmpeg/releases/download/9.1/{_PACKAGE}.zip"
_ARCHIVE_SHA256 = "b9bbbb3acece000cbeec994f856649354e52c773807841e78908374b24d658fd"
_ARCHIVE_SIZE = 119_467_219
_EXECUTABLES = {
    "ffmpeg": "ed8f66cb877087a96f5bbb40a2944f0e764f2ad8870d5cb647360d912e62bc07",
    "ffprobe": "df0199f606faa88e9ad192732a7c3987acfb647663534c297772e17730611a08",
}
_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
_LOCK = Lock()
_verified: dict[Path, tuple[int, int, int]] = {}


class FFmpegDownloadError(RuntimeError):
    pass


def _download(archive: Path) -> None:
    url = httpx.URL(_URL)
    with httpx.Client(timeout=httpx.Timeout(45, connect=10), trust_env=False) as client:
        for _ in range(5):
            if (
                url.scheme != "https"
                or url.host not in _HOSTS
                or url.userinfo
                or url.port not in (None, 443)
            ):
                raise FFmpegDownloadError("Поставщик FFmpeg вернул недоверенный адрес загрузки.")
            with client.stream("GET", url) as response:
                if response.is_redirect and (location := response.headers.get("location")):
                    url = response.url.join(location)
                    continue
                response.raise_for_status()
                count = 0
                digest = hashlib.sha256()
                with archive.open("wb") as stream:
                    for block in response.iter_bytes(256 * 1024):
                        count += len(block)
                        if count > _ARCHIVE_SIZE:
                            raise FFmpegDownloadError("Размер архива FFmpeg не совпадает.")
                        digest.update(block)
                        stream.write(block)
                if count != _ARCHIVE_SIZE or digest.hexdigest() != _ARCHIVE_SHA256:
                    raise FFmpegDownloadError("Контрольная сумма архива FFmpeg не совпадает.")
                return
    raise FFmpegDownloadError("Слишком много переадресаций при загрузке FFmpeg.")


def _valid(path: Path, checksum: str) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    stat = path.stat()
    identity = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if _verified.get(path) != identity:
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != checksum:
                return False
        _verified[path] = identity
    return True


def vendor_tool(name: str) -> str:
    """Use the pinned tools and acquire them from the vendor once if absent."""
    if name not in _EXECUTABLES:
        raise FFmpegDownloadError("Неизвестная программа FFmpeg.")
    profile = os.environ.get("LOCALAPPDATA")
    if not profile:
        raise FFmpegDownloadError("Не найдена локальная папка профиля Windows.")
    directory = Path(profile) / "UchebaAI" / "MediaTools" / _PACKAGE
    target = directory / f"{name}.exe"
    try:
        with _LOCK:
            if _valid(target, _EXECUTABLES[name]):
                return str(target)
            directory.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix=".download-", dir=directory) as work:
                pending = Path(work)
                archive = pending / "ffmpeg.zip"
                _download(archive)
                with ZipFile(archive) as package:
                    for filename in ("ffmpeg.exe", "ffprobe.exe", "LICENSE"):
                        member = (
                            f"{_PACKAGE}/{filename}"
                            if filename == "LICENSE"
                            else f"{_PACKAGE}/bin/{filename}"
                        )
                        with (
                            package.open(member) as source,
                            (pending / filename).open("wb") as dest,
                        ):
                            shutil.copyfileobj(source, dest)
                        if filename != "LICENSE" and not _valid(
                            pending / filename, _EXECUTABLES[Path(filename).stem]
                        ):
                            raise FFmpegDownloadError("Проверка файла FFmpeg не пройдена.")
                    for filename in ("LICENSE", "ffmpeg.exe", "ffprobe.exe"):
                        (pending / filename).replace(directory / filename)
        return str(target)
    except (OSError, httpx.HTTPError) as exc:
        raise FFmpegDownloadError(
            "Не удалось получить FFmpeg. Для первой обработки медиа нужен доступ к GitHub."
        ) from exc
