"""Exercise the 0.3.0 update dialog with live release assets in a clean Windows CI profile."""

import hashlib
import os
import subprocess
import winreg
from pathlib import Path
from time import monotonic, sleep

import httpx
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from ucheba import ui

_UNINSTALL = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    r"\{9A4F80D8-9AD1-4B56-955B-4F261461413C}_is1"
)
_RELEASE = "https://api.github.com/repos/cheden123/ucheba/releases/tags/v0.4.0"


def verified_version(installation: Path) -> str:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _UNINSTALL) as key:
        location = Path(str(winreg.QueryValueEx(key, "InstallLocation")[0]))
        if location.resolve() != installation.resolve():
            raise RuntimeError("Получен каталог, отличный от тестовой установки.")
        return str(winreg.QueryValueEx(key, "DisplayVersion")[0])


def wait_for_release() -> None:
    endpoint = monotonic() + 600
    with httpx.Client(timeout=15) as client:
        while monotonic() < endpoint:
            response = client.get(_RELEASE)
            if response.status_code == 200:
                release = response.json()
                if release.get("draft") is False and release.get("prerelease") is False:
                    return
            elif response.status_code != 404:
                response.raise_for_status()
            sleep(15)
    raise RuntimeError("Стабильный выпуск v0.4.0 не появился за время проверки.")


def main() -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Скрипт установки предназначен для чистого GitHub Windows runner.")

    installation = Path(os.environ["UCHEBA_TEST_INSTALLATION"])
    profile = Path(os.environ["UCHEBA_TEST_WORK"])
    profile.mkdir()
    if verified_version(installation) != "0.3.0":
        raise RuntimeError("Для проверки требуется чистая установка 0.3.0.")
    wait_for_release()

    app = QApplication([])
    settings = QSettings(str(profile / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("qa/upgrade_marker", "preserve")
    settings.sync()
    window = ui.MainWindow(profile / "queue.sqlite3", settings)
    window.paused = True
    # Exercise the installed-mode dialog in an isolated Qt profile after
    # explicitly verifying that the corresponding native 0.3.0 is installed.
    ui.installed_copy = lambda: True
    dialog = ui.UpdateDialog(window)
    dialog.private_update_dir = lambda: profile / "updates"
    window.show()
    dialog.show()

    def wait_for_idle() -> None:
        limit = monotonic() + 240
        while dialog.worker is not None and monotonic() < limit:
            app.processEvents()
            sleep(0.025)
        app.processEvents()
        if dialog.worker is not None:
            raise RuntimeError("Операция обновления не завершилась вовремя.")
        print(dialog.status.text().splitlines()[0], flush=True)

    assert dialog.current_version == "0.3.0"
    dialog.check_button.click()
    wait_for_idle()
    assert dialog.available_update and dialog.available_update.version == "0.4.0"
    dialog.download_button.click()
    wait_for_idle()
    installer = dialog.installer
    assert installer and installer.version == "0.4.0" and installer.path.is_relative_to(profile)

    launches: list[Path] = []
    confirmations: list[str] = []

    def approve() -> None:
        modal = QApplication.activeModalWidget()
        if not isinstance(modal, QMessageBox) or "0.4.0" not in modal.text():
            return
        confirmations.append(modal.text().splitlines()[0])
        for button in modal.buttons():
            if button.text() == "Установить":
                button.click()
                return
        modal.reject()

    def launch_silently(path: Path) -> bool:
        # The worker already rechecked Authenticode, SHA-256 and the size. In
        # this disposable Windows profile use Inno's UI-less install flags.
        if path != installer.path:
            return False
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != installer.sha256:
                return False
        subprocess.run(
            [str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"],
            check=True,
            timeout=180,
        )
        launches.append(path)
        return True

    dialog.launch_installer = launch_silently
    QTimer.singleShot(0, approve)
    dialog.install_button.click()
    wait_for_idle()
    assert confirmations and len(launches) == 1
    assert verified_version(installation) == "0.4.0"
    marker = installation / "_internal" / "ucheba" / "app-version.txt"
    assert marker.read_text(encoding="ascii").strip() == "0.4.0"
    assert {path.name for path in (installation / "_internal").glob("ucheba_ai-*.dist-info")} == {
        "ucheba_ai-0.4.0.dist-info"
    }
    check = QSettings(str(profile / "settings.ini"), QSettings.Format.IniFormat)
    assert check.value("qa/upgrade_marker") == "preserve"
    subprocess.run([str(installation / "UchebaAI.exe"), "--smoke-test"], check=True, timeout=120)
    print("PASS: 0.3.0 → 0.4.0, GitHub, диалог, установка, сохранение профиля.", flush=True)
    dialog.close()
    window.close()
    app.processEvents()


if __name__ == "__main__":
    main()
