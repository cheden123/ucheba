import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from uuid import uuid4


@dataclass(frozen=True)
class Job:
    id: str
    source: Path
    output: Path
    model: str
    device: str
    cloud: bool
    status: str
    message: str
    result: Path | None
    source_identity: str | None = None
    slides: bool = False
    speaker_model: Path | None = None
    state_dir: Path | None = None
    template: str = "academic"


def source_identity(path: Path) -> str:
    stat = path.stat()
    return ":".join(
        str(value)
        for value in (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )
    )


class QueueStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, output TEXT NOT NULL,
                    model TEXT NOT NULL, device TEXT NOT NULL, cloud INTEGER NOT NULL,
                    status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', result TEXT, source_identity TEXT
                )
            """)

            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            for field, sql in (
                ("source_identity", "ALTER TABLE jobs ADD COLUMN source_identity TEXT"),
                (
                    "template",
                    "ALTER TABLE jobs ADD COLUMN template TEXT NOT NULL DEFAULT 'academic'",
                ),
                ("slides", "ALTER TABLE jobs ADD COLUMN slides INTEGER NOT NULL DEFAULT 0"),
                ("speaker_model", "ALTER TABLE jobs ADD COLUMN speaker_model TEXT"),
            ):
                if field not in columns:
                    db.execute(sql)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _job(self, row: sqlite3.Row) -> Job:
        return Job(
            row["id"],
            Path(row["source"]),
            Path(row["output"]),
            row["model"],
            row["device"],
            bool(row["cloud"]),
            row["status"],
            row["message"],
            Path(row["result"]) if row["result"] else None,
            row["source_identity"],
            bool(row["slides"]),
            Path(row["speaker_model"]) if row["speaker_model"] else None,
            self.path.parent / "checkpoints",
            row["template"],
        )

    def add(
        self,
        source: Path,
        output: Path,
        *,
        model: str,
        device: str,
        cloud: bool,
        slides: bool = False,
        speaker_model: Path | None = None,
        template: str = "academic",
    ) -> Job:
        if template not in {"academic", "standard", "compact", "modern"}:
            raise ValueError("Неизвестный шаблон Word.")
        job_id = uuid4().hex
        identity = source_identity(source) if cloud else None
        with self._connect() as db:
            db.execute(
                "INSERT INTO jobs(id, source, output, model, device, cloud, status, source_identity, slides, speaker_model, template) "
                "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
                (
                    job_id,
                    str(source.absolute()),
                    str(output.absolute()),
                    model,
                    device,
                    cloud,
                    identity,
                    slides,
                    str(speaker_model.absolute()) if speaker_model else None,
                    template,
                ),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> Job:
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job(row)

    def list(self) -> list[Job]:
        with self._connect() as db:
            return [self._job(row) for row in db.execute("SELECT * FROM jobs ORDER BY rowid")]

    def claim_next(self) -> Job | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                UPDATE jobs SET status = 'running', message = ''
                WHERE id = (SELECT id FROM jobs WHERE status = 'queued' ORDER BY rowid LIMIT 1)
                RETURNING *
            """).fetchone()
        return self._job(row) if row else None

    def update(self, job_id: str, message: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET message = ? WHERE id = ? AND status = 'running'",
                (message, job_id),
            )

    def finish(
        self, job_id: str, status: str, message: str = "", result: Path | None = None
    ) -> None:
        if status not in {"succeeded", "failed", "cancelled", "queued"}:
            raise ValueError("Недопустимый результат обработки")
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET status = ?, message = ?, result = ? "
                "WHERE id = ? AND (status = 'running' OR "
                "(status = 'cancelled' AND ? = 'succeeded'))",
                (status, message, str(result) if result else None, job_id, status),
            )

    def retry(self, job_id: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET status = 'queued', message = '' "
                "WHERE id = ? AND status IN ('failed', 'cancelled')",
                (job_id,),
            )

    def cancel(self, job_id: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET status = 'cancelled', message = 'Отменено' "
                "WHERE id = ? AND status IN ('queued', 'running')",
                (job_id,),
            )

    def recover(self) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET status = 'queued', message = 'Возобновление обработки' "
                "WHERE status = 'running'"
            )
