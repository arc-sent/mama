import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

import api
import config
import db
import plan_files
import scheduler

TOKEN = "t" * 24


@pytest.fixture
def server(tmp_path):
    video_dir = str(tmp_path / "videos")
    os.makedirs(video_dir)
    srv = api.serve("127.0.0.1", 0, TOKEN, video_dir)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, video_dir, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def call(url, *, token=TOKEN, data=None, headers=None, method=None):
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data is not None else None
    if body:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def make_plan(video_dir, plan_id=1, content=b"0123456789" * 100):
    path = os.path.join(video_dir, f"video{plan_id}.mp4")
    with open(path, "wb") as f:
        f.write(content)
    items = [{"id": plan_id, "file_path": path}]
    plan = {"id": plan_id, "groups": [{"id": 7, "name": "Группа", "videos": [
        {"id": plan_id, "path": f"/c/video/video{plan_id}.mp4", "description": "текст"}]}]}
    plan_files.add_file_info(plan, items)
    db.ensure_user(1)
    db.save_plan(1, "2026-10-12", plan)
    return plan, path


def test_server_refuses_to_start_without_token(tmp_path):
    with pytest.raises(ValueError):
        api.serve("127.0.0.1", 0, "", str(tmp_path))


def test_health_open_everything_else_needs_token(server):
    _, _, base = server
    assert call(base + "/health", token=None)[0] == 200
    for url in ("/plan/latest", "/plan/1", "/video/video1.mp4"):
        assert call(base + url, token=None)[0] == 401
        assert call(base + url, token="wrong")[0] == 401


def test_no_pending_plan_is_404(server):
    assert call(server[2] + "/plan/latest")[0] == 404


def test_latest_plan_has_file_info_and_video_downloads_whole_and_resumes(server):
    _, video_dir, base = server
    plan, _ = make_plan(video_dir)
    status, body, _ = call(base + "/plan/latest")
    got = json.loads(body)
    video = got["groups"][0]["videos"][0]
    assert status == 200 and got["id"] == 1 and got["groups"][0]["name"] == "Группа"
    assert video["file"] == "video1.mp4" and video["size"] == 1000 and len(video["sha256"]) == 64
    assert video["description"] == "текст"

    status, data, _ = call(base + "/video/video1.mp4")
    assert status == 200 and len(data) == 1000
    assert plan_files.hashlib.sha256(data).hexdigest() == video["sha256"]

    status, tail, headers = call(base + "/video/video1.mp4", headers={"Range": "bytes=600-"})
    assert status == 206 and tail == data[600:] and headers["Content-Range"] == "bytes 600-999/1000"
    assert call(base + "/video/video1.mp4", headers={"Range": "bytes=5000-"})[0] == 416


def test_only_own_video_names_are_served(server):
    _, video_dir, base = server
    secret = os.path.join(os.path.dirname(video_dir), "secret.txt")
    open(secret, "w").write("x")
    for name in ("..%2Fsecret.txt", "secret.txt", "video1.mp4.bak", "video.mp4"):
        assert call(f"{base}/video/{name}")[0] == 404


def test_result_closes_the_plan_and_cleanup_removes_files(server):
    _, video_dir, base = server
    _, path = make_plan(video_dir)
    status, _, _ = call(base + "/plan/1/result", data={"id": 1, "p_human": 90})
    assert status == 200
    assert call(base + "/plan/latest")[0] == 404                 # больше не ожидает выкладки
    assert json.loads(call(base + "/plan/1")[1])["acked"] is True
    assert json.loads(db.get_plan(1)["result"])["p_human"] == 90

    config.VIDEO_DIR = video_dir
    assert scheduler.cleanup_acked(keep_days=2) == 0             # ответ только что — рано
    assert os.path.exists(path)
    assert scheduler.cleanup_acked(keep_days=2, now_ts=int(time.time()) + 3 * 86400) == 1
    assert not os.path.exists(path) and db.get_plan(1)["cleaned"] == 1


def test_result_validation(server):
    base = server[2]
    make_plan(server[1])
    assert call(base + "/plan/99/result", data={"a": 1})[0] == 404
    assert call(base + "/plan/1/result", data=[1, 2])[0] == 400
    req = urllib.request.Request(base + "/plan/1/result", data=b"not json",
                                 headers={"Authorization": f"Bearer {TOKEN}"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 400
