import sqlite3

import account_source


def _entries(n):
    return [{"id": str(i), "url": f"https://www.tiktok.com/@u/video/{i}", "title": f"t{i}",
             "duration": 10 + i} for i in range(n)]


def test_parse_all_takes_every_video_and_marks_bottom(tmp_path, monkeypatch):
    seen = {}

    def fake_fetch(account, limit, start=1):
        seen["limit"] = limit
        return "u", _entries(584), "yt-dlp"

    monkeypatch.setattr(account_source, "_fetch", fake_fetch)
    db_path = str(tmp_path / "s.db")
    res = account_source.parse_all("u", db_path)
    assert seen["limit"] == account_source.PARSE_ALL_LIMIT > 200     # not capped at PARSE_LIMIT
    assert res["total"] == 584 and res["added"] == 584 and res["exhausted"] is True
    assert sqlite3.connect(db_path).execute("select count(*) from videos").fetchone()[0] == 584
    assert account_source.parse_all("u", db_path)["added"] == 0        # a second pass adds nothing


def test_parse_all_embed_fallback_is_not_exhausted(tmp_path, monkeypatch):
    monkeypatch.setattr(account_source, "_fetch", lambda a, limit, start=1: ("u", _entries(30), "embed"))
    res = account_source.parse_all("u", str(tmp_path / "s.db"))
    assert res["exhausted"] is False and res["can_deepen"] is False


def test_descriptions_are_saved_and_backfilled(tmp_path):
    import parser_tiktok as pt

    conn = pt.init_db(str(tmp_path / "d.db"))
    # старая запись без описания (как в базах, спарсенных до появления колонки)
    conn.execute("INSERT INTO videos (tt_video_id, username, url, title, duration, found_at) "
                 "VALUES ('1', 'u', 'https://x/1', 'старое', 5, 'now')")
    conn.commit()
    entries = [{"id": "1", "title": "старое", "description": "старое #хэштег", "duration": 5},
               {"id": "2", "title": "t", "description": "полная подпись с #тегами", "duration": 6}]
    assert pt.save_videos(conn, "u", entries) == 1                       # новой только вторая
    rows = dict(conn.execute("SELECT tt_video_id, description FROM videos").fetchall())
    assert rows == {"1": "старое #хэштег", "2": "полная подпись с #тегами"}   # первая дозаполнена
    conn.close()


def test_candidates_carry_description_and_order_by_id(tmp_path):
    import sqlite3
    import source_reader as sr

    p = str(tmp_path / "o.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE videos (tt_video_id TEXT PRIMARY KEY, username TEXT, url TEXT, title TEXT, "
              "duration INTEGER, found_at TEXT, description TEXT)")
    # старый ролик (меньший id) найден ПОЗЖЕ — как при допарсинге истории
    c.executemany("INSERT INTO videos VALUES (?,?,?,?,?,?,?)", [
        ("7700000000000000002", "u", "u2", "t2", 5, "2026-10-09", "новый ролик"),
        ("7600000000000000001", "u", "u1", "t1", 5, "2026-10-12", "старый ролик")])
    c.commit(); c.close()
    by_found = [v["tt_video_id"] for v in sr.fetch_candidates(p, order_dir="old")]
    by_id = [v["tt_video_id"] for v in sr.fetch_candidates(p, order_dir="old", order_by_id=True)]
    assert by_found[0].startswith("77")          # по времени находки первым был бы новый
    assert by_id[0].startswith("76")             # по id — реально старый
    assert sr.fetch_candidates(p, order_by_id=True)[0]["description"] == "старый ролик"
