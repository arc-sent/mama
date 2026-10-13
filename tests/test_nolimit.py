import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
import config
import db
import scheduler
from conftest import make_source_db

MSK = config.MOSCOW_TZ
MORNING = MSK.localize(datetime(2025, 1, 10, 8, 0))


def test_parse_count():
    assert scheduler.parse_count("25", 500) == 25
    assert scheduler.parse_count("+25", 500) == 25
    for bad in ("0", "-3", "abc", "", "501", "2.5", None):
        assert scheduler.parse_count(bad, 500) is None


def test_disk_check():
    gb = 1024 ** 3
    assert scheduler.disk_check(10, 5 * gb, mb_per_video=20) is None          # 0,2 ГБ нужно, свободно 5
    msg = scheduler.disk_check(500, 3 * gb, mb_per_video=20)                  # нужно ~9,8 ГБ
    assert msg and "Не хватит места" in msg and "500" in msg


def _rule(ctx, tmp_path, n_videos, per_day=2):
    path = str(tmp_path / "src.db")
    make_source_db(path, [(f"v{i:03d}", "u", f"https://www.tiktok.com/@u/video/{i}", f"t{i}", 10, f"2024-01-{i:02d}")
                          for i in range(1, n_videos + 1)])
    db.set_source_db_path(ctx["source_id"], path)
    return db.get_rule(db.add_rule(ctx["telegram_id"], ctx["source_id"], ctx["group_id"], videos_per_day=per_day))


def test_extra_larger_than_the_source_gives_what_exists_without_repeats(user_group_source, tmp_path):
    rule = _rule(user_group_source, tmp_path, n_videos=12, per_day=2)
    assert scheduler.plan_rule(rule, MORNING) == 2
    assert scheduler.plan_rule(rule, MORNING, extra=100) == 10            # осталось 10 из 12, больше нет
    ids = [i["tt_video_id"] for i in db.get_queue("pending")]
    assert len(ids) == len(set(ids)) == 12
    assert scheduler.plan_rule(rule, MORNING, extra=5) == 0               # источник исчерпан


def _update_context(args):
    update = MagicMock()
    update.effective_user.id = 1001
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = args
    context.application.bot_data = {}
    context.bot.send_message = AsyncMock()
    return update, context


def test_nolimit_rejects_bad_numbers_and_shows_usage():
    for args in ([], ["abc"], ["0"], [str(config.NOLIMIT_MAX + 1)]):
        update, context = _update_context(args)
        asyncio.run(bot.cmd_nolimit(update, context))
        assert "Использование: /nolimit" in update.message.reply_text.call_args[0][0]
        assert context.application.bot_data == {}


def test_nolimit_runs_in_background_prepares_extra_and_delivers(user_group_source, monkeypatch):
    calls = {}

    async def fake_prepare(app, extra=0, telegram_id=None):
        calls["prepare"] = (extra, telegram_id)

    async def fake_deliver(bot_, uid, **kw):
        calls["deliver"] = uid
        return True

    monkeypatch.setattr(scheduler, "prepare_all", fake_prepare)
    monkeypatch.setattr(scheduler, "deliver_plan", fake_deliver)
    update, context = _update_context(["7"])

    async def scenario():
        await bot.cmd_nolimit(update, context)
        assert 1001 in context.application.bot_data["nolimit_running"]       # пока готовится — занято
        again, ctx2 = _update_context(["3"]); ctx2.application = context.application
        await bot.cmd_nolimit(again, ctx2)
        assert "ещё готовится" in again.message.reply_text.call_args[0][0]   # второй запрос отклонён
        for t in list(context.application.bot_data["tasks"]):
            await t

    asyncio.run(scenario())
    assert calls == {"prepare": (7, 1001), "deliver": 1001}
    assert context.application.bot_data["nolimit_running"] == set()          # блокировка снята
    assert "Готово видео" in context.bot.send_message.call_args_list[0][0][1]


def test_plan_nolimit_alias_goes_to_the_same_handler(monkeypatch):
    seen = {}

    async def fake_nolimit(update, context):
        seen["args"] = list(context.args)

    monkeypatch.setattr(bot, "cmd_nolimit", fake_nolimit)
    update, context = _update_context(["nolimit", "30"])
    asyncio.run(bot.cmd_plan(update, context))
    assert seen["args"] == ["30"]


def test_nolimit_refuses_when_disk_is_too_small(user_group_source, monkeypatch):
    monkeypatch.setattr(bot.scheduler, "disk_check", lambda n, free, mb=None: f"места нет для {n}")
    update, context = _update_context(["50"])
    asyncio.run(bot.cmd_nolimit(update, context))
    assert "места нет" in update.message.reply_text.call_args[0][0]
    assert context.application.bot_data.get("nolimit_running", set()) == set()
