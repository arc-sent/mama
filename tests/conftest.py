"""Тесты не ходят в сеть: DATA_DIR уводим во временную папку ДО импорта db."""

import gc
import os
import pathlib
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="mother_tests_")
os.environ["DATA_DIR"] = _TEST_DATA_DIR
os.environ["VIDEO_DIR"] = os.path.join(_TEST_DATA_DIR, "videos")

import pytest  # noqa: E402

import db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    gc.collect()
    for f in pathlib.Path(_TEST_DATA_DIR).glob("mother.db*"):
        try:
            f.unlink()
        except PermissionError:
            pass
    db.init_db()
    yield
    gc.collect()


@pytest.fixture
def user_group_source():
    uid = 1001
    db.ensure_user(uid)
    db.add_group(uid, vk_group_id=2343, name="Тестовая группа")
    group_id = db.get_groups(uid)[0]["id"]
    source_id = db.add_source(uid, "Источник", "/nonexistent.db", None)
    return {"telegram_id": uid, "group_id": group_id, "source_id": source_id}


def make_source_db(path, videos):
    """videos: (tt_video_id, username, url, title, duration, found_at)."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE videos (tt_video_id TEXT PRIMARY KEY, username TEXT, url TEXT, "
        "title TEXT, duration INTEGER, found_at TEXT)"
    )
    conn.executemany("INSERT INTO videos VALUES (?, ?, ?, ?, ?, ?)", videos)
    conn.commit()
    conn.close()
