import sqlite3
from contextlib import contextmanager, suppress
from pathlib import Path

from platformdirs import user_data_dir

DB_DIR = Path(user_data_dir("torrra"))
DB_FILE = DB_DIR / "torrra.db"


@contextmanager
def get_db_connection():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS torrents (
                magnet_uri TEXT PRIMARY KEY,
                title TEXT,
                size REAL,
                source TEXT,
                is_paused BOOLEAN DEFAULT 0,
                is_notified BOOLEAN DEFAULT 0
            )
            """
        )
        # migration code
        with suppress(sqlite3.OperationalError):
            cursor.execute(
                "ALTER TABLE torrents ADD COLUMN is_notified BOOLEAN DEFAULT 0"
            )

        # transcoding jobs table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS transcode_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                magnet_uri TEXT NOT NULL,
                source_file TEXT NOT NULL,
                destination_file TEXT,
                status TEXT DEFAULT 'pending',
                progress REAL DEFAULT 0,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (magnet_uri) REFERENCES torrents(magnet_uri)
                    ON DELETE CASCADE
            )
            """
        )
        conn.commit()
