"""Exercise the previous version's Qt dialog against the latest public GitHub release."""

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
from ucheba.queue import QueueStore

_UNINSTALL = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    r"\{9A4F80D8-9AD1-4B56-955B-4F261461413C}_is1"
)
_RELEASE = "https://api.github.com/repos/cheden123/ucheba/releases/latest"


def verified_version(installation: Path) -> str:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _UNINSTALL) as key:
        location = Path(str(winreg.QueryValueEx(key, "InstallLocation")[0]))
        if location.resolve() != installation.resolve():
            raise RuntimeError("Получен каталог, отличный от тестовой установки.")
        return str(winreg.QueryValueEx(key, "DisplayVersion")[0])


def wait_for_release(version: str) -> None:
    endpoint = monotonic() + 600
    with httpx.Client(timeout=15) as client:
        while monotonic() < endpoint:
            response = client.get(_RELEASE)
            if response.status_code == 200:
                release = response.json()
                if (
                    release.get("tag_name") == f"v{version}"
                    and release.get("draft") is False
                    and release.get("prerelease") is False
                ):
                    return
            elif response.status_code != 404:
                response.raise_for_status()
            sleep(15)
    raise RuntimeError(f"Стабильный выпуск v{version} не стал последним за время проверки.")


def main() -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Скрипт установки предназначен для чистого GitHub Windows runner.")

    installation = Path(os.environ["UCHEBA_TEST_INSTALLATION"])
    profile = Path(os.environ["UCHEBA_TEST_WORK"])
    previous_version = os.environ["UCHEBA_TEST_PREVIOUS"]
    target_version = os.environ["UCHEBA_TEST_TARGET"]
    profile.mkdir()
    if verified_version(installation) != previous_version:
        raise RuntimeError(f"Для проверки требуется чистая установка {previous_version}.")
    wait_for_release(target_version)

    app = QApplication([])
    settings = QSettings(str(profile / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("qa/upgrade_marker", "preserve")
    settings.sync()
    window = ui.MainWindow(profile / "queue.sqlite3", settings)
    window.paused = True
    queued_job = window.queue.add(
        profile / "sample.mp4", profile / "output", model="tiny", device="cpu", cloud=False
    )
    # Exercise the installed-mode dialog in an isolated Qt profile after
    # verifying the matching installed build via the Windows registry.
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

    launches: list[Path] = []
    confirmations: list[str] = []

    def approve() -> None:
        modal = QApplication.activeModalWidget()
        if not isinstance(modal, QMessageBox):
            return
        if f"обновление {target_version}?" in modal.text():
            for button in modal.buttons():
                if button.text() == "Установить":
                    confirmations.append(modal.text().splitlines()[0])
                    button.click()
                    return
        print(f"Неожиданное окно подтверждения: {modal.text()}", flush=True)
        modal.reject()

    def launch_silently(path: Path) -> bool:
        # The worker already rechecked Authenticode, SHA-256 and the size. In
        # this disposable Windows profile use Inno's UI-less install flags.
        installer = dialog.installer
        if (
            installer is None
            or installer.version != target_version
            or path != installer.path
            or not path.is_relative_to(profile)
        ):
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
    assert dialog.current_version == previous_version
    confirmation_timer = QTimer()
    confirmation_timer.timeout.connect(approve)
    confirmation_timer.start(100)
    try:
        dialog.check_button.click()
        wait_for_idle()
        assert dialog.available_update and dialog.available_update.version == target_version
        # 0.3.0 separates the check, download and install into three buttons.
        if download_button := getattr(dialog, "download_button", None):
            download_button.click()
            wait_for_idle()
            dialog.install_button.click()
            wait_for_idle()
    finally:
        confirmation_timer.stop()
    assert confirmations and len(launches) == 1
    assert verified_version(installation) == target_version
    marker = installation / "_internal" / "ucheba" / "app-version.txt"
    assert marker.read_text(encoding="ascii").strip() == target_version
    assert {path.name for path in (installation / "_internal").glob("ucheba_ai-*.dist-info")} == {
        f"ucheba_ai-{target_version}.dist-info"
    }
    check = QSettings(str(profile / "settings.ini"), QSettings.Format.IniFormat)
    assert check.value("qa/upgrade_marker") == "preserve"
    assert QueueStore(profile / "queue.sqlite3").get(queued_job.id) == queued_job
    subprocess.run([str(installation / "UchebaAI.exe"), "--smoke-test"], check=True, timeout=120)
    print(
        f"PASS: {previous_version} → {target_version}, GitHub, диалог, установка, настройки и очередь.",
        flush=True,
    )
    dialog.close()
    window.close()
    app.processEvents()


if __name__ == "__main__":
    main()
