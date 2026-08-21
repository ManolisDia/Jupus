import sqlite3


def connect(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path, check_same_thread=False)
