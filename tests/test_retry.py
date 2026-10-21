"""Повтор выкладки: кнопка под итогом, план из не вышедших видео, отдельная выдача ПК."""

import asyncio
import json
import os

import config
import db
import retry
import scheduler

BODY = {"id": 11, "accounts": [
    {"id": None, "profile": None, "name": None, "groups": [
        {"id": 100, "name": "История", "videos": [
            {"id": 1, "path": "/c/v/video1.mp4", "file": "video1.mp4", "size": 1, "sha256": "a"}]}]},
    {"id": 555, "profile": "682d1e9fb6f4", "name": "Алексей", "groups": [
        {"id": 200, "name": "Конфитюр", "videos": [
            {"id": 2, "path": "/c/v/video2.mp4", "file": "video2.mp4", "size": 1, "sha256": "b", "description": "д"},
            {"id": 3, "path": "/c/v/video3.mp4", "file": "video3.mp4", "size": 1, "sha256": "c"}]},
        {"id": 300, "name": "А4", "videos": [
            {"id": 4, "path": "/c/v/video4.mp4", "file": "video4.mp4", "size": 1, "sha256": "d"}]}]}]}
RESULT = {"id": 11, "groups": [
    {"id": 100, "name": "История", "videos": [{"id": 1, "status": "ok"}]},
    {"id": 200, "name": "Конфитюр", "account": {"id": 555, "name": "Алексей"}, "videos": [
        {"id": 2, "status": "failed", "error": "VK: «Не удалось опубликовать клип» after 3 tries"},
        {"id": 3, "status": "not_attempted", "error": "the run ended"}]},
    {"id": 300, "name": "А4", "videos": [
        {"id": 4, "status": "failed", "error": "the video is 11:23 long; VK takes clips of 1 s to 3 min"}]}]}


def _files(*names):
    os.makedirs(config.VIDEO_DIR, exist_ok=True)
    for n in names:
        with open(os.path.join(config.VIDEO_DIR, n), "wb") as f:
            f.write(b"x")


def _plan_with_result(user=1001):
    db.ensure_user(user)
    db.save_plan(user, "2026-10-18", BODY)
    db.set_plan_result(11, RESULT)


def test_only_videos_a_retry_can_help_are_chosen():
    assert retry.retryable(RESULT) == [(200, 2), (200, 3)]          # ok и «длиннее 3 минут» — нет
    assert retry.retryable({"groups": [{"id": 1, "videos": [{"id": 9, "status": "pending"}]}]}) == []


def test_retry_plan_keeps_only_those_videos_with_their_data():
    plan = retry.build_retry_plan(BODY, [(200, 2), (200, 3)], 12, 11)
    assert plan["id"] == 12 and plan["retry_of"] == 11
    assert [a["id"] for a in plan["accounts"]] == [555]               # аккаунт без повторов убран
    g = plan["accounts"][0]["groups"]
    assert [x["id"] for x in g] == [200] and [v["id"] for v in g[0]["videos"]] == [2, 3]
    assert g[0]["videos"][0]["description"] == "д" and g[0]["videos"][0]["file"] == "video2.mp4"
    assert [v["id"] for v in BODY["accounts"][1]["groups"][0]["videos"]] == [2, 3]   # исходник не тронут


def test_button_is_offered_only_when_a_retry_helps():
    m = scheduler.retry_markup(11, RESULT)
    btn = m.inline_keyboard[0][0]
    assert btn.callback_data == "retry:11" and "(2)" in btn.text
    assert scheduler.retry_markup(11, {"groups": [{"id": 1, "videos": [{"id": 1, "status": "ok"}]}]}) is None


def test_create_retry_makes_a_separate_plan_for_the_pc():
    _files("video2.mp4", "video3.mp4")
    _plan_with_result()
    text = scheduler.create_retry(1001, 11)
    assert "повтор плана №11" in text and "видео 2" in text
    row = db.get_pending_retry()
    plan = json.loads(row["body"])
    assert row["retry_of"] == 11 and plan["retry_of"] == 11 and row["id"] > 11
    assert db.get_pending_plan() is None or db.get_pending_plan()["retry_of"] is None   # ежедневная выдача отдельно
    assert "уже создан" in scheduler.create_retry(1001, 11)          # дважды не создаём
    assert scheduler.create_retry(2002, 11) == "Такого плана нет."    # чужой план


def test_retry_skips_videos_whose_files_are_gone():
    _files("video2.mp4")                                             # video3.mp4 уже удалён
    for f in ("video3.mp4",):
        p = os.path.join(config.VIDEO_DIR, f)
        if os.path.exists(p):
            os.remove(p)
    _plan_with_result()
    text = scheduler.create_retry(1001, 11)
    assert "не повторяются: видео 3" in text
    plan = json.loads(db.get_pending_retry()["body"])
    assert [v["id"] for a in plan["accounts"] for g in a["groups"] for v in g["videos"]] == [2]


def test_daily_plan_and_retry_are_served_separately():
    _files("video2.mp4", "video3.mp4")
    _plan_with_result()
    scheduler.create_retry(1001, 11)
    db.save_plan(1001, "2026-10-19", {"id": 50, "accounts": []})       # завтрашний ежедневный план
    assert db.get_pending_plan()["id"] == 50
    assert db.get_pending_retry()["retry_of"] == 11


def test_notification_of_a_retry_says_so_and_files_in_use_survive_cleanup():
    _files("video2.mp4", "video3.mp4")
    _plan_with_result()
    scheduler.create_retry(1001, 11)

    class Bot:
        sent = []

        async def send_message(self, chat_id, text, reply_markup=None):
            self.sent.append((text, reply_markup))

    asyncio.run(scheduler.notify_results(Bot()))
    assert Bot.sent[0][1] is not None                                 # под итогом плана 11 — кнопка
    # итог плана 11 «старый», но его видео нужны невыложенному повтору — не удаляются
    removed = scheduler.cleanup_acked(keep_days=0, now_ts=10**12)
    assert os.path.exists(os.path.join(config.VIDEO_DIR, "video2.mp4")) and removed == 0
    retry_id = db.get_pending_retry()["id"]
    db.set_plan_result(retry_id, {"groups": [{"id": 200, "videos": [{"id": 2, "status": "ok"}]}]})
    asyncio.run(scheduler.notify_results(Bot()))
    assert "(повтор плана №11)" in Bot.sent[-1][0] and Bot.sent[-1][1] is None
