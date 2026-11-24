import hashlib
from pathlib import Path

import httpx
import pytest

from ucheba import updates


def test_check_and_download_published_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = b"installer bytes"
    base = "https://github.com/cheden123/ucheba/releases/download/v1.10.2"
    setup = "UchebaAI-Setup-x64.exe"
    checksum = "SHA256SUMS.txt"
    manifest = f"{hashlib.sha256(installer).hexdigest()}  {setup}\n".encode()
    accessed = []

    def reply(request: httpx.Request) -> httpx.Response:
        accessed.append(str(request.url))
        if request.url.host == "api.github.com":
            return httpx.Response(
                200,
                json={
                    "tag_name": "v1.10.2",
                    "draft": False,
                    "prerelease": False,
                    "body": "Изменения",
                    "assets": [
                        {
                            "name": name,
                            "size": size,
                            "browser_download_url": f"{base}/{name}",
                        }
                        for name, size in [(setup, len(installer)), (checksum, len(manifest))]
                    ],
                },
            )
        if request.url == httpx.URL(f"{base}/{checksum}"):
            return httpx.Response(200, content=manifest)
        if request.url == httpx.URL(f"{base}/{setup}"):
            return httpx.Response(
                302, headers={"Location": "https://release-assets.githubusercontent.com/setup"}
            )
        assert request.url.host == "release-assets.githubusercontent.com"
        return httpx.Response(200, content=installer)

    monkeypatch.setattr(updates, "_verify_signature", lambda path: None)
    transport = httpx.MockTransport(reply)
    update = updates.check_update("1.2.0", transport=transport)
    assert update is not None
    assert update.version == "1.10.2"
    assert update.notes == "Изменения"
    path = updates.download_update(update, tmp_path, transport=transport)
    assert path.read_bytes() == installer
    assert len(accessed) == 4


def test_disallows_untrusted_asset_redirection(tmp_path: Path) -> None:
    update = updates.Update("1.0.0", "", "", 10)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"Location": "https://example.net/file"})
    )
    with pytest.raises(updates.UpdateError, match="неизвестный адрес"):
        updates.download_update(update, tmp_path, transport=transport)
    assert not list(tmp_path.iterdir())


def test_discards_an_installer_with_mismatched_checksum(tmp_path: Path) -> None:
    update = updates.Update("1.0.0", "", "", 2)

    def reply(request: httpx.Request) -> httpx.Response:
        payload = (
            f"{'0' * 64}  UchebaAI-Setup-x64.exe\n".encode()
            if request.url.path.endswith("SHA256SUMS.txt")
            else b"MZ"
        )
        return httpx.Response(200, content=payload)

    with pytest.raises(updates.UpdateError, match="сумма"):
        updates.download_update(update, tmp_path, transport=httpx.MockTransport(reply))
    assert not list(tmp_path.iterdir())


def test_missing_release_can_be_distinguished_from_current() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    assert updates.check_update("0.1.0", transport=transport) is None
    with pytest.raises(updates.NoPublicRelease):
        updates.check_update("0.1.0", transport=transport, require_public=True)


def _local_release(folder: Path, content: bytes, *, checksum: str | None = None) -> Path:
    folder.mkdir()
    source = folder / "UchebaAI-Setup-x64.exe"
    source.write_bytes(content)
    checksum = checksum or hashlib.sha256(content).hexdigest()
    (folder / "SHA256SUMS.txt").write_text(f"{checksum}  {source.name}\n", encoding="utf-8")
    return source


def test_offline_stages_and_rechecks_local_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _local_release(tmp_path / "release", b"MZ original")
    monkeypatch.setattr(updates, "_installer_version", lambda path: "0.10.0")
    monkeypatch.setattr(updates, "_verify_signature", lambda path: False)
    staged = updates.stage_offline_update(source.parent, tmp_path / "cache", "0.9.1")
    assert staged.version == "0.10.0"
    assert not staged.signed
    assert staged.path != source
    source.write_bytes(b"MZ modified")
    assert updates.verify_installer_for_launch(staged).read_bytes() == b"MZ original"
    staged.path.write_bytes(b"MZ modified")
    with pytest.raises(updates.UpdateError, match="Контрольная сумма"):
        updates.verify_installer_for_launch(staged)


@pytest.mark.parametrize("bad_hash", [False, True])
def test_rejects_offline_version_or_mismatched_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_hash: bool
) -> None:
    source = _local_release(tmp_path / "release", b"MZ", checksum="0" * 64 if bad_hash else None)
    monkeypatch.setattr(updates, "_installer_version", lambda path: "1.0.0")
    cache = tmp_path / "cache"
    with pytest.raises(updates.UpdateError, match="сумма" if bad_hash else "новее"):
        updates.stage_offline_update(source.parent, cache, "1.0.0")
    assert not list(cache.iterdir())
