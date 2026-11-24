from pathlib import Path

from ucheba.diagnostics import export_log, record_event
from ucheba.queue import QueueStore


def test_diagnostics_only_export_curated_events(tmp_path: Path) -> None:
    queue = QueueStore(tmp_path / "profile" / "queue.db")
    secret = "sk-or-v1-should-never-appear"
    filename = "Confidential lecture.mp4"
    job = queue.add(
        tmp_path / filename, tmp_path / "Private", model=secret, device="cpu", cloud=False
    )
    record_event(queue, "job_start", job=job)
    record_event(queue, secret)
    record_event(queue, "key_check_ok")
    report = tmp_path / "log.txt"
    export_log(queue, report)
    contents = report.read_text(encoding="utf-8-sig")
    assert "Запущено задание" in contents and "OpenRouter принял ключ" in contents
    assert "cpu" in contents
    assert secret not in contents and filename not in contents and str(tmp_path) not in contents
