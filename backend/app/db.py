import os
import sqlite3
from pathlib import Path

_DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data"
_DEFAULT_FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", _DEFAULT_DATA))


def fixtures_dir() -> Path:
    return Path(os.environ.get("FIXTURES_DIR", _DEFAULT_FIXTURES))


def db_path() -> Path:
    return data_dir() / "clickspan.db"


def connect() -> sqlite3.Connection:
    data_dir().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
