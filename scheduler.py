"""Планировщик mother.

Утром (PREPARE_HOUR МСК) и при старте: для каждого включённого правила берём
остаток дневной нормы неотданных видео из источника, скачиваем, обрабатываем и
складываем в VIDEO_DIR (очередь, статус ready). В DELIVER_HOUR:DELIVER_MINUTE
собираем из готовых файлов JSON-план и отправляем его файлом в Telegram.
Очередь хранится в БД и переживает рестарт.
"""

import asyncio
import json
import logging
import os
import shutil
import traceback as tb_module
from datetime import datetime, time as dtime, timedelta

import config
import db
import notify
import plan_files
import video_processor
from config import MOSCOW_TZ
from downloader import (
    detect_platform, download_tiktok, download_likee, download_youtube,
)
from plan_builder import build_plan

logger = logging.getLogger(__name__)

PLATFORM_LABELS = {"tiktok": "TikTok", "likee": "Likee", "youtube": "YouTube Shorts"}

# ─── Глобальная пауза (/stop, /startBot) ──────────────────────────────────────
# Автоматические подготовка и выдача на паузе не работают; ручной /plan работает.
_PAUSED_KEY = "paused"


def is_paused() -> bool:
    return db.get_setting(_PAUSED_KEY, "0") == "1"


def set_paused(paused: bool) -> None:
    db.set_setting(_PAUSED_KEY, "1" if paused else "0")


_video_semaphore: asyncio.Semaphore | None = None


def _process_semaphore() -> asyncio.Semaphore:
    global _video_semaphore
    if _video_semaphore is None:
        _video_semaphore = asyncio.Semaphore(config.VIDEO_PROCESS_CONCURRENCY)
    return _video_semaphore


def _record_error(telegram_id, exc, *, stage=None, platform=None, url=None,
                  group_name=None) -> None:
    db.log_error(
        telegram_id, stage=stage, platform=platform, url=url, group_name=group_name,
        message=str(exc),
        traceback="".join(tb_module.format_exception(type(exc), exc, exc.__traceback__)),
    )


# ─── Дни и время ──────────────────────────────────────────────────────────────

def delivery_day(now: datetime | None = None) -> str:
    """День ближайшей выдачи (YYYY-MM-DD, МСК): сегодня, если время выдачи ещё не наступило, иначе завтра."""
    now = now or datetime.now(MOSCOW_TZ)
    deliver_at = now.replace(hour=config.DELIVER_HOUR, minute=config.DELIVER_MINUTE,
                             second=0, microsecond=0)
    day = now.date() if now < deliver_at else now.date() + timedelta(days=1)
    return day.isoformat()


# ─── Скачивание и обработка ───────────────────────────────────────────────────

async def _download(url: str) -> tuple[str, str]:
    platform = detect_platform(url)
    if platform == "tiktok":
        return await download_tiktok(url, None)
    if platform == "likee":
        return await download_likee(url)
    if platform == "youtube":
        return await download_youtube(url, None)
    raise ValueError(f"Не удалось определить платформу по ссылке: {url}")


async def _process(file_path: str, telegram_id: int, *, url: str | None = None,
                   platform: str | None = None, group_name: str | None = None) -> tuple[str, str]:
    """Прогнать ролик через video_processor согласно настройке.

    Сбой обработки не срывает выдачу — остаётся оригинал, ошибка идёт в /errors.
    """
    mode = db.get_setting(video_processor.SETTING_MODE, video_processor.OFF)
    if not video_processor.is_enabled(mode):
        return file_path, ""
    downscale = db.get_setting(video_processor.SETTING_DOWNSCALE, "0") == "1"

    loop = asyncio.get_running_loop()
    try:
        async with _process_semaphore():
            new_path, status, detail = await loop.run_in_executor(
                None, video_processor.process_video, file_path, mode, downscale
            )
    except Exception as exc:
        logger.exception("Обработка видео: непредвиденный сбой")
        _record_error(telegram_id, exc, stage="обработка видео", platform=platform,
                      url=url, group_name=group_name)
        return file_path, "⚠️ обработка не удалась — оригинал"

    if status == "done":
        video_processor.cleanup(file_path)
        return new_path, f"🎬 {detail}"
    if status == "skipped":
        return file_path, f"🎬 без обработки ({detail})"
    if status == "failed":
        _record_error(telegram_id, RuntimeError(detail), stage="обработка видео",
                      platform=platform, url=url, group_name=group_name)
        return file_path, "⚠️ обработка не удалась — оригинал"
    return file_path, ""


# ─── Планирование правила ─────────────────────────────────────────────────────

def plan_rule(rule, now: datetime | None = None, extra: int = 0) -> int:
    """Поставить в очередь остаток дневной нормы правила. Вернуть число добавленных.

    Норма — N видео на день выдачи: считаем всё, что уже стоит в очереди на этот
    день (в том числе выданное), и добираем только остаток. Иначе стартовый прогон
    и утренний job задвоили бы выдачу.

    ``extra`` > 0 — принудительно взять ещё столько видео СВЕРХ нормы (для проверок). Такие
    видео помечаются днём ``<день>+extra`` и в норму не засчитываются: завтрашняя
    выдача от них не пострадает. Повторы по-прежнему исключаются.
    """
    from source_reader import fetch_candidates, SourceError

    rule_id = rule["id"]
    day = delivery_day(now)
    if extra > 0:
        remaining, plan_day = extra, f"{day}+extra"
    else:
        remaining, plan_day = rule["videos_per_day"] - db.count_queue_for_day(rule_id, day), day
    if remaining <= 0:
        return 0

    def fetch(exclude):
        return fetch_candidates(
            rule["db_path"], username=rule["username"],
            min_duration=rule["min_duration"], max_duration=rule["max_duration"],
            order_dir=rule["order_dir"], exclude_ids=exclude, limit=remaining,
            order_by_id=rule["source_kind"] == "account",
        )

    try:
        candidates = fetch(db.get_delivered_ids(rule_id) | db.get_queued_ids(rule_id))
        if not candidates and rule["allow_repost"]:
            # Источник исчерпан + перезаливка: сбрасываем историю и идём по кругу.
            db.clear_delivered(rule_id)
            logger.info("Правило %s (%s → %s): источник исчерпан, сброс для перезаливки",
                        rule_id, rule["source_name"], rule["group_name"])
            candidates = fetch(db.get_queued_ids(rule_id))
    except SourceError as e:
        logger.warning("Правило %s: источник недоступен: %s", rule_id, e)
        _record_error(rule["telegram_id"], e, stage="чтение источника",
                      group_name=rule["group_name"])
        return 0

    added = 0
    for video in candidates:
        item_id = db.add_queue_item(
            telegram_id=rule["telegram_id"], rule_id=rule_id,
            tt_video_id=video["tt_video_id"], url=video["url"],
            title=video["title"], plan_day=plan_day, video_description=video.get("description"),
        )
        if item_id is not None:
            added += 1
    logger.info("Правило %s (%s → %s): в очередь на %s добавлено %s видео",
                rule_id, rule["source_name"], rule["group_name"], day, added)
    return added


async def prepare_queue() -> tuple[int, int]:
    """Скачать и обработать все pending-видео. Вернуть (готово, пропущено)."""
    os.makedirs(config.VIDEO_DIR, exist_ok=True)
    ready = skipped = 0
    for item in db.get_queue("pending"):
        rule = db.get_rule(item["rule_id"])
        group_name = rule["group_name"] if rule else None
        platform = detect_platform(item["url"])
        file_path = None
        try:
            file_path, _title = await _download(item["url"])
            file_path, _note = await _process(
                file_path, item["telegram_id"], url=item["url"],
                platform=platform, group_name=group_name,
            )
            final = os.path.join(config.VIDEO_DIR, f"video{item['id']}.mp4")
            shutil.move(file_path, final)
            file_path = None
            db.mark_queue_ready(item["id"], final)
            ready += 1
        except Exception as exc:
            logger.exception("Не удалось подготовить видео %s", item["url"])
            _record_error(item["telegram_id"], exc, stage="подготовка видео",
                          platform=platform, url=item["url"], group_name=group_name)
            # Битую ссылку не берём повторно: помечаем выданной и снимаем с очереди.
            if rule:
                db.mark_delivered(item["rule_id"], item["tt_video_id"])
            db.delete_queue_item(item["id"])
            skipped += 1
        finally:
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass
    return ready, skipped


# ─── Источники-аккаунты ───────────────────────────────────────────────────────

async def refresh_account_sources(context_or_app) -> int:
    """До-парсить все источники-аккаунты. Блокирующий парсер уводим в executor;
    ошибка одного аккаунта не мешает остальным."""
    import account_source

    sources = db.get_account_sources()
    if not sources:
        return 0
    loop = asyncio.get_running_loop()
    refreshed = 0
    for s in sources:
        db_path = s["db_path"] or account_source.source_db_path(s["id"])
        start = s["parse_start"] if "parse_start" in s.keys() else 1
        try:
            res = await loop.run_in_executor(
                None, account_source.refresh_account, s["account"], db_path,
                account_source.PARSE_LIMIT, start,
            )
            refreshed += 1
            logger.info("Источник %s (@%s): +%s новых", s["id"], res["username"], res["added"])
        except Exception as exc:
            logger.exception("Не удалось спарсить источник %s (%s)", s["id"], s["account"])
            _record_error(s["telegram_id"], exc, stage="парсинг аккаунта", platform="tiktok")
    return refreshed


def _min_remaining(src_rules, cap: int) -> int:
    """Минимальный запас неотданных кандидатов среди правил источника (слабое звено)."""
    from source_reader import fetch_candidates

    worst = cap
    for r in src_rules:
        exclude = db.get_delivered_ids(r["id"]) | db.get_queued_ids(r["id"])
        cands = fetch_candidates(
            r["db_path"], username=r["username"],
            min_duration=r["min_duration"], max_duration=r["max_duration"],
            order_dir=r["order_dir"], exclude_ids=exclude, limit=cap,
        )
        worst = min(worst, len(cands))
        if worst == 0:
            break
    return worst


async def ensure_backlog(context_or_app) -> int:
    """Догрузить историю аккаунтов, у чьих правил кандидаты на исходе.

    Если запас меньше BACKLOG_DAYS дневных норм — углубляемся в историю, пока запас
    не наберётся или не упрёмся в дно аккаунта (тогда ставим backfill_done).
    """
    import account_source
    from source_reader import SourceError

    account_sources = db.get_account_sources()
    if not account_sources:
        return 0
    rules_by_source: dict[int, list] = {}
    for r in db.get_enabled_rules():
        rules_by_source.setdefault(r["source_id"], []).append(r)

    loop = asyncio.get_running_loop()
    deepened = 0
    for src in account_sources:
        if src["backfill_done"]:
            continue
        src_rules = rules_by_source.get(src["id"])
        if not src_rules:
            continue
        db_path = src["db_path"] or account_source.source_db_path(src["id"])
        need = max(r["videos_per_day"] for r in src_rules) * config.BACKLOG_DAYS

        for _ in range(config.DEEPEN_MAX_PASSES):
            try:
                remaining = _min_remaining(src_rules, need)
            except SourceError:
                break
            if remaining >= need:
                break
            try:
                res = await loop.run_in_executor(
                    None, account_source.deepen_account, src["account"], db_path
                )
            except Exception as exc:
                logger.exception("Не удалось углубить источник %s (%s)",
                                 src["id"], src["account"])
                _record_error(src["telegram_id"], exc,
                              stage="допарсинг истории", platform="tiktok")
                break
            deepened += 1
            logger.info("Источник %s (@%s): углубление +%s новых (запас был %s/%s)",
                        src["id"], res["username"], res["added"], remaining, need)
            if res["exhausted"]:
                db.set_backfill_done(src["id"])
                break
            if not res["can_deepen"] or res["added"] == 0:
                break
    return deepened


async def prepare_all(app, extra: int = 0, telegram_id: int | None = None) -> None:
    """Утренняя подготовка: до-парсинг, планирование и скачивание (с догоном при сбоях).

    extra > 0 — взять по каждому правилу (пользователя telegram_id, если задан) ещё столько
    видео сверх дневной нормы; только в первом раунде, повторные раунды — обычные."""
    try:
        await refresh_account_sources(app)
        await ensure_backlog(app)
    except Exception:
        logger.exception("Ошибка до-парсинга аккаунтов")

    loop = asyncio.get_running_loop()
    for round_no in range(config.PREPARE_ROUNDS):
        planned = 0
        for rule in db.get_enabled_rules():
            if telegram_id is not None and rule["telegram_id"] != telegram_id:
                continue
            try:
                n_extra = extra if round_no == 0 else 0
                planned += await loop.run_in_executor(None, plan_rule, rule, None, n_extra)
            except Exception:
                logger.exception("Ошибка планирования правила %s", rule["id"])
        # pending могли остаться и от прошлого прогона (рестарт посреди скачивания)
        if not planned and not db.get_queue("pending"):
            break
        ready, skipped = await prepare_queue()
        logger.info("Подготовка: готово %s, пропущено %s", ready, skipped)
        if not skipped:
            break


# ─── Выдача плана ─────────────────────────────────────────────────────────────

PLANS_DIR = os.path.join(db.DATA_DIR, "plans")


def parse_count(arg: str, maximum: int) -> int | None:
    """'25' / '+25' -> 25; None, если это не число от 1 до maximum."""
    arg = (arg or "").strip().lstrip("+")
    return int(arg) if arg.isdigit() and 1 <= int(arg) <= maximum else None


def disk_check(n_videos: int, free_bytes: int, mb_per_video: int | None = None) -> str | None:
    """Текст отказа, если места на диске заведомо не хватит на n_videos; иначе None."""
    mb = mb_per_video or config.NOLIMIT_MB_PER_VIDEO
    need, spare = n_videos * mb * 1024 * 1024, 512 * 1024 * 1024   # +0,5 ГБ запаса
    if free_bytes - spare >= need:
        return None
    return (f"Не хватит места на диске: для {n_videos} видео нужно примерно {need / 1e9:.1f} ГБ "
            f"(по {mb} МБ на видео), свободно {free_bytes / 1e9:.1f} ГБ. Возьмите меньше.")


def quota_report(telegram_id: int, now: datetime | None = None) -> str:
    """Сколько видео по каждому включённому правилу уже выдано на ближайший день выдачи."""
    day = delivery_day(now)
    lines = []
    for rule in db.get_rules(telegram_id):
        if rule["enabled"]:
            used = db.count_queue_for_day(rule["id"], day)
            lines.append(f"• {rule['source_name']} → {rule['group_name']}: {used} из {rule['videos_per_day']}")
    return f"Дневная норма на {day}:\n" + "\n".join(lines) if lines else ""


async def deliver_plan(bot, telegram_id: int, *, notify_empty: bool = True) -> bool:
    """Собрать JSON из готовых видео пользователя и отправить файлом. True — отправлено."""
    items = db.get_queue("ready", telegram_id)
    if not items:
        if notify_empty:
            text = "📭 План пуст: готовых видео нет."
            report = quota_report(telegram_id)
            if report:
                text += ("\n\n" + report + "\n\nЕсли норма уже выдана, новых видео до следующего дня не будет. "
                         "Чтобы получить ещё сейчас: /plan 2 (ещё 2 видео сверх нормы по каждому правилу) "
                         "или увеличьте «видео в день» в правиле.")
            try:
                await bot.send_message(telegram_id, text)
            except Exception:
                logger.exception("Не удалось отправить уведомление о пустом плане")
        return False

    plan_id = db.next_plan_id()
    plan = build_plan(plan_id, items, config.OUTPUT_PATH_PREFIX)
    text = json.dumps(plan, ensure_ascii=False, indent=2)
    day = datetime.now(MOSCOW_TZ).date().isoformat()

    os.makedirs(PLANS_DIR, exist_ok=True)
    plan_file = os.path.join(PLANS_DIR, f"plan_{plan_id}.json")
    with open(plan_file, "w", encoding="utf-8") as f:
        f.write(text)

    n_videos = sum(len(g["videos"]) for g in plan["groups"])
    with open(plan_file, "rb") as f:
        await bot.send_document(
            telegram_id, document=f, filename=f"plan_{plan_id}.json",
            caption=f"📦 План #{plan_id}: групп {len(plan['groups'])}, видео {n_videos}",
        )

    # Только после успешной отправки: сохраняем план и помечаем видео выданными. В базе лежит
    # расширенный вариант (название группы, описания, имя/размер/сумма файлов) — его отдаёт API
    # для ПК; в Telegram ушёл прежний короткий формат.
    ext = build_plan(plan_id, items, config.OUTPUT_PATH_PREFIX, extended=True)
    ext = await asyncio.get_running_loop().run_in_executor(None, plan_files.add_file_info, ext, items)
    db.save_plan(telegram_id, day, ext)
    for it in items:
        db.mark_delivered(it["rule_id"], it["tt_video_id"])
        db.mark_queue_delivered(it["id"])
    logger.info("План #%s отправлен пользователю %s", plan_id, telegram_id)
    return True


async def send_test_plan(bot, telegram_id: int) -> None:
    """ТЕСТ: прислать плашку с JSON немедленно, ничего не меняя в БД.

    Берёт готовые видео, если они есть; иначе строит пример из групп пользователя
    (или из группы 2343). Видео не помечаются выданными, id плана не расходуется.
    """
    items = db.get_queue("ready", telegram_id)
    if items:
        plan = build_plan(0, items, config.OUTPUT_PATH_PREFIX)
        src = "из готовых видео"
    else:
        groups = db.get_groups(telegram_id)
        gids = [g["vk_group_id"] for g in groups] or [2343]
        fake, n = [], 1
        for gid in gids:
            for _ in range(3):
                fake.append({"id": n, "vk_group_id": gid, "file_path": f"video{n}.mp4"})
                n += 1
        plan = build_plan(0, fake, config.OUTPUT_PATH_PREFIX)
        src = "пример (готовых видео нет)"

    text = json.dumps(plan, ensure_ascii=False, indent=2)
    os.makedirs(PLANS_DIR, exist_ok=True)
    plan_file = os.path.join(PLANS_DIR, "test_plan.json")
    with open(plan_file, "w", encoding="utf-8") as f:
        f.write(text)
    with open(plan_file, "rb") as f:
        await bot.send_document(
            telegram_id, document=f, filename="test_plan.json",
            caption=f"🧪 ТЕСТОВЫЙ план — {src}. В БД ничего не изменено.",
        )


def cleanup_acked(keep_days: int | None = None, now_ts: int | None = None) -> int:
    """Удалить с диска видео планов, результат по которым ПК прислал давно. Вернуть число файлов.

    Видео нужны серверу, пока ПК их не забрал и не отчитался; потом они только занимают место."""
    import time

    keep_days = config.KEEP_DAYS if keep_days is None else keep_days
    cutoff = (now_ts if now_ts is not None else int(time.time())) - keep_days * 86400
    removed = 0
    for row in db.get_plans_to_clean(cutoff):
        for name in plan_files.video_files(json.loads(row["body"])):
            path = plan_files.safe_video_path(config.VIDEO_DIR, name)
            if path:
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    logger.warning("не удалось удалить %s", path)
        db.mark_plan_cleaned(row["id"])
    if removed:
        logger.info("Очистка: удалено видео %s", removed)
    return removed


async def cleanup_job(context) -> None:
    cleanup_acked()


async def notify_results(bot) -> int:
    """Написать в Telegram об итогах выкладки, которые прислал ПК и о которых ещё не писали.

    Помечает план как «сообщено» только после успешной отправки: при сбое Telegram попробуем снова.
    Возвращает число отправленных уведомлений."""
    sent = 0
    for row in db.get_unnotified_results():
        try:
            text = notify.format_result(row["id"], json.loads(row["result"]))
        except Exception:
            logger.exception("Итог плана %s не разобрать — помечаю как сообщённый", row["id"])
            db.mark_plan_notified(row["id"])
            continue
        try:
            await bot.send_message(row["telegram_id"], text)
        except Exception:
            logger.exception("Не удалось отправить итог плана %s, повторю позже", row["id"])
            continue
        db.mark_plan_notified(row["id"])
        sent += 1
        logger.info("Итог плана %s отправлен пользователю %s", row["id"], row["telegram_id"])
    return sent


async def notify_job(context) -> None:
    await notify_results(context.bot)


async def deliver_job(context) -> None:
    if is_paused():
        logger.info("Выдача плана пропущена: пауза")
        return
    for uid in db.get_user_ids():
        try:
            await deliver_plan(context.bot, uid)
        except Exception:
            logger.exception("Не удалось отправить план пользователю %s", uid)


async def prepare_job(context) -> None:
    if is_paused():
        logger.info("Подготовка пропущена: пауза")
        return
    await prepare_all(context.application)


# ─── Регистрация и восстановление ─────────────────────────────────────────────

async def _catch_up_job(context) -> None:
    """Стартовый догон: подготовить видео и, если время выдачи уже прошло, а план
    сегодня не отправлялся, отдать готовое."""
    if is_paused():
        return
    await prepare_all(context.application)
    now = datetime.now(MOSCOW_TZ)
    deliver_at = now.replace(hour=config.DELIVER_HOUR, minute=config.DELIVER_MINUTE,
                             second=0, microsecond=0)
    if now < deliver_at:
        return
    today = now.date().isoformat()
    for uid in db.get_user_ids():
        if db.get_plan_for_day(uid, today):
            continue
        try:
            await deliver_plan(context.bot, uid, notify_empty=False)
        except Exception:
            logger.exception("Догоняющая выдача не удалась для %s", uid)


async def restore_and_schedule(app) -> None:
    """post_init: очередь уже в БД; догон запускаем фоновым job'ом, чтобы не тормозить старт."""
    os.makedirs(config.VIDEO_DIR, exist_ok=True)
    app.job_queue.run_once(_catch_up_job, when=5, name="catch_up")


def register_jobs(app) -> None:
    # итоги выкладки с ПК -> сообщение в Telegram (проверка раз в 20 секунд)
    app.job_queue.run_repeating(notify_job, interval=20, first=10, name="notify_results")
    app.job_queue.run_daily(
        cleanup_job, time=dtime(hour=4, minute=0, tzinfo=MOSCOW_TZ), name="cleanup")
    app.job_queue.run_daily(
        prepare_job,
        time=dtime(hour=config.PREPARE_HOUR, minute=config.PREPARE_MINUTE, tzinfo=MOSCOW_TZ),
        name="prepare",
    )
    app.job_queue.run_daily(
        deliver_job,
        time=dtime(hour=config.DELIVER_HOUR, minute=config.DELIVER_MINUTE, tzinfo=MOSCOW_TZ),
        name="deliver",
    )
