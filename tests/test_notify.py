import asyncio
import json

import db
import notify
import scheduler

RESULT = {
    "id": 3, "p_human": 86, "verdict": "человек", "finished_at": "2026-10-13T15:29:31",
    "feed_breaks": [{"minutes": 2.1, "likes": 1, "shares": 1}, {"minutes": 3.4, "likes": 4, "shares": 2}],
    "groups": [{"id": 7, "name": "КИТИК ФАН", "videos": [
        {"id": 5, "status": "ok", "site_video_id": 10088},
        {"id": 6, "status": "failed", "error": "site: слишком много загрузок"}]}],
    "missing": [{"id": 8, "reason": "не скачалось"}],
}


def test_summary_text_has_everything_important():
    text = notify.format_result(3, RESULT)
    assert text.startswith("⚠️ Выкладка плана №3 завершена")             # часть выложена — предупреждение
    assert "Выложено: 1 из 3" in text                                     # 2 в плане + 1 не скачалось
    assert "✅ КИТИК ФАН: видео 5, id на сайте 10088" in text
    assert "❌ КИТИК ФАН: видео 6 — site: слишком много загрузок" in text
    assert "⚠️ Не скачалось на ПК: видео 8 — не скачалось" in text
    assert "Оценка человекоподобности: 86% (человек)" in text
    assert "2 перерыв(а), 5,5 мин, лайков 5, репостов 3" in text
    assert "Завершено: 2026-10-13 15:29:31" in text


def test_headline_by_outcome():
    ok = {"groups": [{"name": "Г", "videos": [{"id": 1, "status": "ok"}, {"id": 2, "status": "ok"}]}]}
    none = {"groups": [{"name": "Г", "videos": [{"id": 1, "status": "failed"}]}]}
    assert notify.format_result(1, ok).startswith("✅")
    assert notify.format_result(1, none).startswith("❌")
    assert "Оценка" not in notify.format_result(1, ok)                    # нет данных — нет строки
    assert notify.format_result(1, {}).startswith("❌")                   # пустой итог не роняет


class Bot:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send_message(self, chat_id, text):
        if self.fail:
            raise RuntimeError("telegram недоступен")
        self.sent.append((chat_id, text))


def _plan_with_result(user=1001, plan_id=3):
    db.ensure_user(user)
    db.save_plan(user, "2026-10-13", {"id": plan_id, "groups": []})
    db.set_plan_result(plan_id, RESULT)


def test_notification_sent_once_to_the_owner():
    _plan_with_result()
    bot = Bot()
    assert asyncio.run(scheduler.notify_results(bot)) == 1
    chat, text = bot.sent[0]
    assert chat == 1001 and "плана №3" in text
    assert asyncio.run(scheduler.notify_results(bot)) == 0               # повторно не пишем
    assert len(bot.sent) == 1


def test_failed_send_is_retried_later():
    _plan_with_result()
    assert asyncio.run(scheduler.notify_results(Bot(fail=True))) == 0
    assert len(db.get_unnotified_results()) == 1                         # не потеряно
    good = Bot()
    assert asyncio.run(scheduler.notify_results(good)) == 1 and len(good.sent) == 1


def test_plan_without_result_is_not_announced():
    db.ensure_user(1001)
    db.save_plan(1001, "2026-10-13", {"id": 4, "groups": []})
    assert asyncio.run(scheduler.notify_results(Bot())) == 0


def test_results_from_before_the_feature_are_not_resent(tmp_path):
    # база, созданная до появления колонки notified: итог есть, уведомление уже «не нужно»
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("DROP TABLE plans")
    conn.execute("CREATE TABLE plans (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL, "
                 "plan_day TEXT NOT NULL, created_at INTEGER NOT NULL, body TEXT NOT NULL, result TEXT, "
                 "acked_at INTEGER, cleaned INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO plans (id, telegram_id, plan_day, created_at, body, result, acked_at) "
                 "VALUES (1, 1001, 'd', 0, '{}', '{}', 0)")
    conn.commit(); conn.close()
    db.init_db()                                                         # миграция
    assert db.get_unnotified_results() == []
