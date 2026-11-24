from pathlib import Path

from ucheba.queue import QueueStore


def test_delete_preserves_files_and_remembers_watch_exclusion(tmp_path: Path) -> None:
    queue = QueueStore(tmp_path / "queue.db")
    source = tmp_path / "lecture.mp4"
    source.write_bytes(b"original")
    output = tmp_path / "results"
    output.mkdir()
    report = output / "lecture.docx"
    report.write_bytes(b"word")
    job = queue.add(source, output, model="base", device="cpu", cloud=False)
    assert queue.claim_next() is not None
    assert not queue.delete(job.id)
    queue.finish(job.id, "succeeded", result=output)
    assert queue.delete(job.id)
    assert not queue.delete(job.id)
    reopened = QueueStore(queue.path)
    assert reopened.list() == []
    assert source in reopened.watch_exclusions()
    assert source.read_bytes() == b"original"
    assert report.read_bytes() == b"word"
    assert reopened.add(source, output, model="base", device="cpu", cloud=False).id != job.id


def test_delete_queued_job_prevents_claim(tmp_path: Path) -> None:
    queue = QueueStore(tmp_path / "queue.db")
    job = queue.add(tmp_path / "missing.mp4", tmp_path, model="base", device="cpu", cloud=False)
    assert queue.delete(job.id)
    assert queue.claim_next() is None
