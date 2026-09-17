import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DATABASE = DATA_DIR / "telepost.db"


class SchedulerDB:
    def __init__(self):
        self.database = DATABASE
        self._create_tables()

    def _connect(self):
        connection = sqlite3.connect(
            self.database,
            timeout=30,
        )
        connection.row_factory = sqlite3.Row

        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")

        return connection

    def _create_tables(self):
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    run_at REAL,
                    interval_seconds INTEGER,
                    data TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                )
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_jobs_active
                ON jobs(active)
                """
            )

            connection.commit()

    def create_job(
        self,
        job_type,
        run_at,
        interval_seconds,
        data,
    ):
        job_id = str(uuid.uuid4())[:8]
        created_at = datetime.now().timestamp()

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id,
                    job_type,
                    run_at,
                    interval_seconds,
                    data,
                    created_at,
                    active
                )
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    job_id,
                    job_type,
                    run_at,
                    interval_seconds,
                    json.dumps(data),
                    created_at,
                ),
            )

            connection.commit()

        return job_id

    def get_job(self, job_id):
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT *
                FROM jobs
                WHERE id = ?
                AND active = 1
                """,
                (job_id,),
            ).fetchone()

    def get_active_jobs(self):
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT *
                FROM jobs
                WHERE active = 1
                ORDER BY
                    CASE
                        WHEN run_at IS NULL
                        THEN 9999999999
                        ELSE run_at
                    END
                """
            ).fetchall()

    def delete_job(self, job_id):
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET active = 0
                WHERE id = ?
                AND active = 1
                """,
                (job_id,),
            )

            connection.commit()

            return cursor.rowcount > 0

    def update_run_time(self, job_id, run_at):
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET run_at = ?
                WHERE id = ?
                AND active = 1
                """,
                (
                    run_at,
                    job_id,
                ),
            )

            connection.commit()

    def delete_one_time_job(self, job_id):
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET active = 0
                WHERE id = ?
                """,
                (job_id,),
            )

            connection.commit()