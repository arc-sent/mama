import asyncio
import json
from datetime import datetime

import config
import db
import scheduler
from conftest import make_source_db

MSK = config.MOSCOW_TZ
MORNING = MSK.localize(datetime(2025, 1, 10, 8, 0))
AFTERNOON = MSK.localize(datetime(2025, 1, 10, 13, 0))


def _rule(ctx, tmp_path, n_videos=5, per_day=2):
    path = str(tmp_path / "src.db")
    make_source_db(path, [
        (f"v{i}", "u", f"https://www.tiktok.com/@u/video/{i}", f"t{i}", 10, f"2024-01-0{i}")
        for i in range(1, n_videos + 1)
    ])
    db.set_source_db_path(ctx["source_id"], path)
    rid = db.add_rule(ctx["telegram_id"], ctx["source_id"], ctx["group_id"],
                      videos_per_day=per_day)
    return db.get_rule(rid)


def _deliver_all_pending():
    for it in db.get_queue("pending"):
        db.mark_delivered(it["rule_id"], it["tt_video_id"])
        db.mark_queue_delivered(it["id"])


def test_delivery_day_before_and_after_deliver_time():
    assert scheduler.delivery_day(MORNING) == "2025-01-10"
    assert scheduler.delivery_day(AFTERNOON) == "2025-01-11"


def test_daily_quota_not_doubled(user_group_source, tmp_path):
    rule = _rule(user_group_source, tmp_path, per_day=2)
    assert scheduler.plan_rule(rule, MORNING) == 2
    assert scheduler.plan_rule(rule, MORNING) == 0
    assert len(db.get_queue("pending")) == 2


def test_quota_survives_delivery(user_group_source, tmp_path):
    rule = _rule(user_group_source, tmp_path, per_day=2)
    scheduler.plan_rule(rule, MORNING)
    _deliver_all_pending()
    assert scheduler.plan_rule(rule, MORNING) == 0


def test_next_day_takes_new_videos(user_group_source, tmp_path):
    rule = _rule(user_group_source, tmp_path, per_day=2)
    scheduler.plan_rule(rule, MORNING)
    _deliver_all_pending()
    assert scheduler.plan_rule(rule, AFTERNOON) == 2
    assert {i["tt_video_id"] for i in db.get_queue("pending")} == {"v3", "v4"}


def test_repost_resets_when_exhausted(user_group_source, tmp_path):
    rule = _rule(user_group_source, tmp_path, n_videos=2, per_day=2)
    db.update_rule(rule["id"], allow_repost=1)
    rule = db.get_rule(rule["id"])
    db.mark_delivered(rule["id"], "v1")
    db.mark_delivered(rule["id"], "v2")
    assert scheduler.plan_rule(rule, MORNING) == 2


class FakeBot:
    def __init__(self):
        self.documents, self.messages = [], []

    async def send_document(self, chat_id, document, filename=None, caption=None):
        self.documents.append((chat_id, filename, json.load(document)))

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


def _make_ready(tmp_path):
    for it in db.get_queue("pending"):
        db.mark_queue_ready(it["id"], str(tmp_path / f"video{it['id']}.mp4"))


def test_deliver_plan_sends_json_and_marks_delivered(user_group_source, tmp_path):
    uid = user_group_source["telegram_id"]
    rule = _rule(user_group_source, tmp_path, per_day=2)
    scheduler.plan_rule(rule, MORNING)
    _make_ready(tmp_path)

    bot = FakeBot()
    assert asyncio.run(scheduler.deliver_plan(bot, uid)) is True
    chat, name, body = bot.documents[0]
    assert chat == uid and name == "plan_1.json" and body["id"] == 1
    assert body["groups"][0]["id"] == 2343
    assert [v["path"] for v in body["groups"][0]["videos"]] == [
        f"{config.OUTPUT_PATH_PREFIX}/video1.mp4", f"{config.OUTPUT_PATH_PREFIX}/video2.mp4"]
    assert db.get_queue("ready") == []
    assert db.get_delivered_ids(rule["id"]) == {"v1", "v2"}
    # повторно: готового нет — файл не шлётся, приходит сообщение «пусто»
    assert asyncio.run(scheduler.deliver_plan(bot, uid)) is False
    assert len(bot.documents) == 1 and bot.messages


def test_plan_ids_increment(user_group_source, tmp_path):
    uid = user_group_source["telegram_id"]
    rule = _rule(user_group_source, tmp_path, per_day=1)
    bot = FakeBot()
    for day in (10, 11):
        scheduler.plan_rule(rule, MSK.localize(datetime(2025, 1, day, 8, 0)))
        _make_ready(tmp_path)
        asyncio.run(scheduler.deliver_plan(bot, uid))
    assert [d[2]["id"] for d in bot.documents] == [1, 2]


# ─── принудительно сверх нормы и объяснение пустого плана ──────────────────────

def test_extra_videos_go_beyond_quota_and_do_not_eat_tomorrows(user_group_source, tmp_path):
    rule = _rule(user_group_source, tmp_path, n_videos=8, per_day=2)
    assert scheduler.plan_rule(rule, MORNING) == 2                  # обычная норма
    assert scheduler.plan_rule(rule, MORNING) == 0                  # больше нормы нет
    assert scheduler.plan_rule(rule, MORNING, extra=3) == 3         # а принудительно — есть
    assert db.count_queue_for_day(rule["id"], "2025-01-10") == 2    # норма дня не изменилась
    days = sorted({i["plan_day"] for i in db.get_queue("pending")})
    assert days == ["2025-01-10", "2025-01-10+extra"]
    assert len({i["tt_video_id"] for i in db.get_queue("pending")}) == 5     # повторов нет
    assert scheduler.plan_rule(rule, MORNING, extra=0) == 0         # обычный прогон по-прежнему пуст


def test_empty_plan_explains_the_daily_quota(user_group_source, tmp_path):
    uid = user_group_source["telegram_id"]
    rule = _rule(user_group_source, tmp_path, per_day=2)
    scheduler.plan_rule(rule, MORNING)
    _deliver_all_pending()
    bot = FakeBot()
    assert asyncio.run(scheduler.deliver_plan(bot, uid)) is False
    text = bot.messages[0][1]
    assert "План пуст" in text and "из 2" in text and "/plan 2" in text
    assert scheduler.quota_report(uid, MORNING).startswith("Дневная норма на 2025-01-10")


# ─── обнуление дневной нормы (/resetquota) ────────────────────────────────────

def test_reset_quota_frees_the_day_without_repeating_delivered_videos(user_group_source, tmp_path):
    uid = user_group_source["telegram_id"]
    rule = _rule(user_group_source, tmp_path, n_videos=8, per_day=2)
    assert scheduler.plan_rule(rule, MORNING) == 2                   # норма дня выбрана
    first = {i["tt_video_id"] for i in db.get_queue("pending")}
    _deliver_all_pending()                                           # и выдана (как ручной /plan)
    assert scheduler.plan_rule(rule, MORNING) == 0                   # завтра бы ничего не дали

    day, freed = scheduler.reset_quota(uid, MORNING)
    assert (day, freed) == ("2025-01-10", 2)
    assert db.count_queue_for_day(rule["id"], "2025-01-10") == 0     # норма снова пуста

    assert scheduler.plan_rule(rule, MORNING) == 2                   # свежая порция
    second = {i["tt_video_id"] for i in db.get_queue("pending")}
    assert not (first & second)                                      # прежние видео не повторились
    assert scheduler.plan_rule(rule, MORNING) == 0                   # а норма снова ограничена


def test_reset_quota_touches_only_this_day_and_user(user_group_source, tmp_path):
    uid = user_group_source["telegram_id"]
    rule = _rule(user_group_source, tmp_path, n_videos=8, per_day=2)
    scheduler.plan_rule(rule, MORNING)                               # день 2025-01-10
    scheduler.plan_rule(rule, MORNING, extra=1)                      # 2025-01-10+extra
    db.ensure_user(2002)
    assert scheduler.reset_quota(2002, MORNING)[1] == 0              # у другого пользователя нечего сбрасывать
    assert scheduler.reset_quota(uid, MORNING)[1] == 2               # extra-запись не затронута
    days = sorted({i["plan_day"] for i in db.get_queue("pending")})
    assert days == ["2025-01-10+extra", "2025-01-10+reset"]


def test_resetquota_command_reports_both_cases(user_group_source, tmp_path):
    import bot
    from unittest.mock import AsyncMock, MagicMock

    rule = _rule(user_group_source, tmp_path, n_videos=8, per_day=2)
    scheduler.plan_rule(rule, scheduler.datetime.now(MSK))           # реальный «сейчас» — как в боте
    update = MagicMock(); update.effective_user.id = user_group_source["telegram_id"]
    update.message.reply_text = AsyncMock()
    asyncio.run(bot.cmd_resetquota(update, MagicMock()))
    assert "обнулена" in update.message.reply_text.call_args[0][0]
    asyncio.run(bot.cmd_resetquota(update, MagicMock()))
    assert "и так пуста" in update.message.reply_text.call_args[0][0]
