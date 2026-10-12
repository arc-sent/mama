"""Выгрузка плана в файл для скрипта выкладки — без Telegram.

Берёт включённые правила, ставит в очередь недостающие видео, скачивает их в VIDEO_DIR
и пишет JSON-план (с названием группы и описанием из правила) в файл, который читает
сценарий ``krug_upload`` (``--plan``).

    python export_plan.py --count 1 --out ..\\plans\\mother_plan.json

По умолчанию видео остаются «готовыми» (``ready``) и не считаются выданными: тот же файл
можно выгрузить повторно. С ``--commit`` видео помечаются выданными, как после отправки
плана в Telegram, а план сохраняется в БД."""

import argparse
import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import config  # noqa: E402
import db  # noqa: E402
import scheduler  # noqa: E402
from plan_builder import build_plan  # noqa: E402

DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plans", "mother_plan.json")


def say(text: str) -> None:
    print(text, flush=True)


def prepare(count: int) -> list:
    """Довести число готовых видео до count: запланировать, скачать, повторить при сбоях."""
    for attempt in range(1, config.PREPARE_ROUNDS + 1):
        ready = db.get_queue("ready")
        need = count - len(ready)
        if need <= 0:
            break
        say(f"нужно ещё видео: {need} (готовых {len(ready)}), попытка {attempt}")
        for rule in db.get_enabled_rules():
            scheduler.plan_rule(rule)
        pending = db.get_queue("pending")
        for extra in pending[need:]:     # лишние ещё не скачаны — просто снимаем с очереди
            db.delete_queue_item(extra["id"])
        pending = pending[:need]
        if not pending:
            say("ставить в очередь нечего: нет правил или кандидатов")
            break
        for it in pending:
            say(f"качаю: {it['url']}")
        ok, skipped = asyncio.run(scheduler.prepare_queue())
        say(f"скачано {ok}, пропущено {skipped}")
    return db.get_queue("ready")[:count]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Выгрузить план mother в JSON-файл для скрипта выкладки.")
    ap.add_argument("--count", type=int, default=1, help="сколько видео в плане (по умолчанию 1)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="куда записать JSON")
    ap.add_argument("--commit", action="store_true",
                    help="пометить видео выданными и сохранить план в БД")
    a = ap.parse_args()

    db.init_db()
    items = prepare(a.count)
    if not items:
        say("готовых видео нет — план не записан")
        return 1

    plan = build_plan(db.next_plan_id(), items, config.OUTPUT_PATH_PREFIX, extended=True)
    out = os.path.abspath(a.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    say(f"план #{plan['id']} записан: {out}")
    for it in items:
        say(f"  видео {it['id']}: {it['file_path']} -> группа {it['group_name']} ({it['vk_group_id']})")

    if a.commit:
        for uid in {it["telegram_id"] for it in items}:
            db.save_plan(uid, scheduler.delivery_day(), plan)
        for it in items:
            db.mark_delivered(it["rule_id"], it["tt_video_id"])
            db.mark_queue_delivered(it["id"])
        say("видео помечены выданными")
    return 0


if __name__ == "__main__":
    sys.exit(main())
