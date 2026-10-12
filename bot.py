"""Telegram-интерфейс mother: группы, источники, правила, план.

Бот ничего не публикует и не знает про VK API: он готовит видео и раз в день
(в DELIVER_HOUR:DELIVER_MINUTE МСК) присылает JSON-план файлом.

Запуск: python bot.py  (из папки mother)
"""

import asyncio
import logging
import os
import re

from dotenv import load_dotenv

load_dotenv()

from telegram import (  # noqa: E402
    InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update,
)
from telegram.ext import (  # noqa: E402
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, TypeHandler, filters,
)

import account_source  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import scheduler  # noqa: E402
import source_reader  # noqa: E402
import video_processor  # noqa: E402
import vk_link  # noqa: E402

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# httpx пишет каждый запрос к Telegram вместе с адресом, а в адресе — токен бота: в логи не нужно
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("mother")

BTN_GROUPS, BTN_SOURCES, BTN_RULES = "👥 Группы", "🎬 Источники", "📋 Правила"
BTN_STATUS, BTN_PLAN = "📊 Статус", "📦 План сейчас"
BTN_TEMPLATES = "📝 Описания"
MENU_TEXTS = {BTN_GROUPS, BTN_SOURCES, BTN_RULES, BTN_STATUS, BTN_PLAN, BTN_TEMPLATES}
MENU = ReplyKeyboardMarkup(
    [[BTN_GROUPS, BTN_SOURCES, BTN_RULES], [BTN_STATUS, BTN_PLAN], [BTN_TEMPLATES]], resize_keyboard=True
)

(G_LINK, G_CONFIRM, G_NAME, G_RENAME, T_TITLE, T_BODY, S_NICK, S_PATH, S_USER,
 R_SRC, R_GRP, R_N, R_EDIT_VALUE, R_DESC_PICK) = range(14)


def _kb(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row]
                                 for row in rows])


async def _reply(update: Update, text: str, markup=None) -> None:
    """Ответить и на сообщение, и на нажатие inline-кнопки."""
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def _show(update: Update, text: str, markup=None) -> None:
    """Правит сообщение с inline-кнопкой (нажатие) или отвечает новым (текст)."""
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, reply_markup=markup)
        except Exception:  # то же содержимое / сообщение устарело
            await q.message.reply_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


# ─── Доступ ───────────────────────────────────────────────────────────────────

async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Если задан ADMIN_IDS — бот отвечает только им."""
    user = update.effective_user
    if user is None:
        return
    if config.ADMIN_IDS and user.id not in config.ADMIN_IDS:
        raise ApplicationHandlerStop
    db.ensure_user(user.id)


def _is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


# ─── Старт, статус ────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 mother готовит видео и в "
        f"{config.DELIVER_HOUR:02d}:{config.DELIVER_MINUTE:02d} МСК присылает JSON-план.\n\n"
        "1) 👥 добавь группы (числовой id)\n2) 🎬 добавь источники\n"
        "3) 📋 свяжи их правилами\n\n/plan — получить план прямо сейчас.",
        reply_markup=MENU,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    rules = db.get_rules(uid)
    ready = db.get_queue("ready", uid)
    pending = db.get_queue("pending", uid)
    state = "⏸ на паузе" if scheduler.is_paused() else "▶️ работает"
    await update.message.reply_text(
        f"{state}\n"
        f"Групп: {len(db.get_groups(uid))}, источников: {len(db.get_sources(uid))}, "
        f"правил: {len(rules)} (включено {sum(1 for r in rules if r['enabled'])})\n"
        f"В очереди: готово {len(ready)}, скачивается {len(pending)}\n"
        f"Выдача: {config.DELIVER_HOUR:02d}:{config.DELIVER_MINUTE:02d} МСК, "
        f"подготовка: {config.PREPARE_HOUR:02d}:{config.PREPARE_MINUTE:02d} МСК",
        reply_markup=MENU,
    )


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выдать план сейчас: если очередь пуста — сперва подготовить."""
    uid = update.effective_user.id
    if not db.get_queue("ready", uid):
        await update.message.reply_text("⏳ Готовых видео нет — готовлю, это может занять время…")
        await scheduler.prepare_all(context.application)
    await scheduler.deliver_plan(context.bot, uid)


async def cmd_testplan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ТЕСТ: мгновенно прислать плашку с JSON, ничего не меняя в БД."""
    await scheduler.send_test_plan(context.bot, update.effective_user.id)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return
    scheduler.set_paused(True)
    await update.message.reply_text("⏸ Автоматические подготовка и выдача остановлены. /startBot — вернуть.")


async def cmd_start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return
    scheduler.set_paused(False)
    await update.message.reply_text("▶️ Автоматическая работа возобновлена.")


async def cmd_errors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    rows = db.get_errors(uid, limit=8)
    if not rows:
        await update.message.reply_text("Ошибок нет ✅")
        return
    lines = [f"Последние ошибки (всего {db.count_errors(uid)}):"]
    for r in rows:
        when = scheduler.datetime.fromtimestamp(r["created_at"], config.MOSCOW_TZ)
        lines.append(f"\n• {when:%d.%m %H:%M} [{r['stage'] or '-'}] {r['group_name'] or ''}\n"
                     f"  {(r['message'] or '')[:200]}\n  {r['url'] or ''}")
    await update.message.reply_text("\n".join(lines)[:4000])


async def cmd_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/process [off|ultrafast|superfast|veryfast|faster] [down|nodown] — режим ffmpeg."""
    if not _is_admin(update.effective_user.id):
        return
    args = context.args
    modes = {video_processor.OFF, "ultrafast", "superfast", "veryfast", "faster"}
    if args and args[0] in modes:
        db.set_setting(video_processor.SETTING_MODE, args[0])
    if len(args) > 1 and args[1] in ("down", "nodown"):
        db.set_setting(video_processor.SETTING_DOWNSCALE, "1" if args[1] == "down" else "0")
    mode = db.get_setting(video_processor.SETTING_MODE, video_processor.OFF)
    down = db.get_setting(video_processor.SETTING_DOWNSCALE, "0") == "1"
    await update.message.reply_text(
        f"🎬 Обработка: {mode}, даунскейл: {'да' if down else 'нет'}\n"
        "Изменить: /process <off|ultrafast|superfast|veryfast|faster> [down|nodown]"
    )


# ─── Диалог: общие выходы ─────────────────────────────────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Отменено.", reply_markup=MENU)
    return ConversationHandler.END


async def menu_escape(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка меню посреди диалога: выходим из него и открываем нужный раздел."""
    context.user_data.clear()
    await MENU_ROUTES[update.message.text](update, context)
    return ConversationHandler.END


# ─── Группы ───────────────────────────────────────────────────────────────────

def groups_kb(telegram_id: int) -> InlineKeyboardMarkup:
    rows = []
    for g in db.get_groups(telegram_id):
        rows.append([InlineKeyboardButton(f"{g['name']} (id {g['vk_group_id']})",
                                          callback_data="noop")])
        rows.append([InlineKeyboardButton("✏️ Переименовать", callback_data=f"g_ren:{g['id']}"),
                     InlineKeyboardButton("🗑 Удалить", callback_data=f"g_del:{g['id']}")])
    rows.append([InlineKeyboardButton("➕ Добавить группу", callback_data="g_add")])
    return InlineKeyboardMarkup(rows)


async def show_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show(update, "Твои группы:", groups_kb(update.effective_user.id))


async def g_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _show(update,
                "Пришли ссылку на сообщество VK:\n"
                "• vk.com/club123456\n• vk.com/public123456\n• vk.com/my_group\n"
                "(можно и просто числовой id)")
    return G_LINK


async def g_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loop = asyncio.get_running_loop()
    gid, name, error = await loop.run_in_executor(None, vk_link.resolve_group, update.message.text)
    logger.info("Группа по ссылке %r -> id=%s name=%s error=%s", update.message.text, gid, name, bool(error))
    if error:
        await update.message.reply_text(error + "\n\nПопробуй ещё раз или /cancel.")
        return G_LINK
    context.user_data["g_vk_id"] = gid
    if name:
        context.user_data["g_name"] = name
        await update.message.reply_text(
            f"Нашёл: «{name}» (id {gid}). Сохранить?",
            reply_markup=_kb([[("✅ Сохранить", "g_ok")], [("✏️ Задать своё имя", "g_own")]]),
        )
        return G_CONFIRM
    await update.message.reply_text(f"Сообщество найдено (id {gid}). Введи название вручную:")
    return G_NAME


async def g_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    if q.data == "g_ok":
        db.add_group(uid, context.user_data.pop("g_vk_id"), context.user_data.pop("g_name"))
        await q.edit_message_text("✅ Группа добавлена.\n\nТвои группы:", reply_markup=groups_kb(uid))
        return ConversationHandler.END
    await q.edit_message_text("Введи название группы вручную:")
    return G_NAME


async def g_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    db.add_group(uid, context.user_data.pop("g_vk_id"), update.message.text.strip()[:100])
    context.user_data.pop("g_name", None)
    await update.message.reply_text("✅ Группа добавлена.\n\nТвои группы:", reply_markup=groups_kb(uid))
    return ConversationHandler.END


async def g_ren(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    gid = int(update.callback_query.data.split(":")[1])
    g = db.get_group(gid)
    if not g or g["telegram_id"] != update.effective_user.id:
        await update.callback_query.answer("Группа не найдена")
        return ConversationHandler.END
    context.user_data["g_rename"] = gid
    await _show(update, "Введи новое название группы:")
    return G_RENAME


async def g_rename_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    db.rename_group(context.user_data.pop("g_rename"), update.message.text.strip()[:100])
    await update.message.reply_text("✅ Переименовано.\n\nТвои группы:", reply_markup=groups_kb(uid))
    return ConversationHandler.END


async def g_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    gid = int(update.callback_query.data.split(":")[1])
    g = db.get_group(gid)
    if g and g["telegram_id"] == update.effective_user.id:
        db.delete_group(gid)
    await update.callback_query.answer("Удалено (вместе с правилами группы)")
    await _show(update, "Твои группы:", groups_kb(update.effective_user.id))


# ─── Заготовки описаний ───────────────────────────────────────────────────────

def templates_kb(telegram_id: int) -> InlineKeyboardMarkup:
    rows = []
    for t in db.get_templates(telegram_id):
        rows.append([InlineKeyboardButton(f"📝 {t['title']}", callback_data="noop")])
        rows.append([InlineKeyboardButton("✏️ Изменить", callback_data=f"t_edit:{t['id']}"),
                     InlineKeyboardButton("🗑 Удалить", callback_data=f"t_del:{t['id']}")])
    rows.append([InlineKeyboardButton("➕ Добавить заготовку", callback_data="t_add")])
    return InlineKeyboardMarkup(rows)


async def show_templates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show(update, "Твои заготовки описаний:", templates_kb(update.effective_user.id))


async def t_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("t_edit", None)
    await _show(update, "Введи название заготовки (короткое, для себя):")
    return T_TITLE


async def t_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tid = int(update.callback_query.data.split(":")[1])
    t = db.get_template(tid)
    if not t or t["telegram_id"] != update.effective_user.id:
        await update.callback_query.answer("Заготовка не найдена")
        return ConversationHandler.END
    context.user_data["t_edit"] = tid
    await _show(update, "Введи новое название заготовки:")
    return T_TITLE


async def t_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["t_title"] = update.message.text.strip()[:100]
    await update.message.reply_text("Теперь введи текст описания:")
    return T_BODY


async def t_body(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    title = context.user_data.pop("t_title", "")
    edit_id = context.user_data.pop("t_edit", None)
    if edit_id:
        db.update_template(edit_id, title, update.message.text)
        msg = "✅ Заготовка обновлена."
    else:
        db.add_template(uid, title, update.message.text)
        msg = "✅ Заготовка добавлена."
    await update.message.reply_text(f"{msg}\n\nТвои заготовки описаний:", reply_markup=templates_kb(uid))
    return ConversationHandler.END


async def t_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = int(update.callback_query.data.split(":")[1])
    t = db.get_template(tid)
    if t and t["telegram_id"] == update.effective_user.id:
        db.delete_template(tid)
    await update.callback_query.answer("Удалено")
    await _show(update, "Твои заготовки описаний:", templates_kb(update.effective_user.id))


# ─── Источники ────────────────────────────────────────────────────────────────

async def show_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sources = db.get_sources(update.effective_user.id)
    lines = ["🎬 Источники:"] + [
        f"• {s['name']} ({'TikTok @' + s['account'] if s['kind'] == 'account' else 'база парсера'})"
        for s in sources
    ] if sources else ["Источников пока нет."]
    rows = []
    for s in sources:
        row = [(f"🗑 {s['name']}", f"s_del:{s['id']}")]
        if s["kind"] == "account":
            row.insert(0, (f"📥 Всё {s['name']}", f"s_all:{s['id']}"))
            row.insert(0, (f"🔄 {s['name']}", f"s_ref:{s['id']}"))
        rows.append(row)
    rows.append([("➕ TikTok-аккаунт", "s_add_acc"), ("➕ База парсера", "s_add_db")])
    await _reply(update, "\n".join(lines), _kb(rows))


async def s_add_acc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _reply(update, "Ник или ссылка на TikTok-аккаунт (например @name):")
    return S_NICK


async def s_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    try:
        username, _ = account_source.normalize(update.message.text.strip())
    except Exception as e:
        await update.message.reply_text(f"Не разобрал аккаунт: {e}\nПопробуй ещё раз или /cancel.")
        return S_NICK
    sid = db.add_source(uid, f"@{username}", "", None, kind="account", account=username)
    path = account_source.source_db_path(sid)
    db.set_source_db_path(sid, path)
    msg = await update.message.reply_text("⏳ Парсю весь аккаунт, это может занять минуту…")
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(None, account_source.parse_all, username, path)
        if res["exhausted"]:
            db.set_backfill_done(sid)       # история собрана целиком — углублять нечего
        await msg.edit_text(f"✅ @{res['username']}: найдено {res['total']}, новых {res['added']}."
                            + (" Вся история собрана." if res["exhausted"] else
                               " Собраны только последние ролики — полную историю TikTok не отдал."))
    except Exception as e:
        logger.exception("Парсинг нового источника не удался")
        await msg.edit_text(f"⚠️ Источник создан, но спарсить не вышло: {e}\n"
                            "Повторится при подготовке или по кнопке 🔄.")
    await update.message.reply_text("Дальше — 📋 Правила.", reply_markup=MENU)
    return ConversationHandler.END


async def s_add_db(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _reply(update, "Полный путь к SQLite-базе парсера (на этом компьютере):")
    return S_PATH


async def s_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    path = update.message.text.strip().strip('"')
    try:
        count = source_reader.check_source(path)
    except source_reader.SourceError as e:
        await update.message.reply_text(f"❌ {e}\nПопробуй ещё раз или /cancel.")
        return S_PATH
    context.user_data["s_path"] = path
    await update.message.reply_text(
        f"В базе {count} видео. Фильтр по нику внутри базы? Введи ник или «-» без фильтра:"
    )
    return S_USER


async def s_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    username = None if raw in ("-", "") else raw.lstrip("@")
    path = context.user_data.pop("s_path")
    name = os.path.splitext(os.path.basename(path))[0] + (f" ({username})" if username else "")
    db.add_source(update.effective_user.id, name, path, username)
    await update.message.reply_text("✅ Источник добавлен.", reply_markup=MENU)
    return ConversationHandler.END


async def s_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sid = int(update.callback_query.data.split(":")[1])
    s = db.get_source(sid)
    if s and s["telegram_id"] == update.effective_user.id:
        db.delete_source(sid)
    await _reply(update, "🗑 Источник удалён вместе с его правилами.")


async def s_ref(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sid = int(update.callback_query.data.split(":")[1])
    s = db.get_source(sid)
    if not s or s["telegram_id"] != update.effective_user.id or s["kind"] != "account":
        return
    await update.callback_query.answer("Парсю…")
    path = s["db_path"] or account_source.source_db_path(sid)
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(None, account_source.refresh_account, s["account"], path)
        await update.callback_query.message.reply_text(
            f"✅ @{res['username']}: найдено {res['total']}, новых {res['added']}.")
    except Exception as e:
        await update.callback_query.message.reply_text(f"⚠️ Не вышло: {e}")


async def s_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """📥 Всё: спарсить всю историю аккаунта, а не только последние PARSE_LIMIT роликов."""
    sid = int(update.callback_query.data.split(":")[1])
    s = db.get_source(sid)
    if not s or s["telegram_id"] != update.effective_user.id or s["kind"] != "account":
        return
    await update.callback_query.answer("Парсю всю историю…")
    path = s["db_path"] or account_source.source_db_path(sid)
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(None, account_source.parse_all, s["account"], path)
        if res["exhausted"]:
            db.set_backfill_done(sid)
        await update.callback_query.message.reply_text(
            f"✅ @{res['username']}: найдено {res['total']}, новых {res['added']}."
            + (" Вся история собрана." if res["exhausted"] else
               " Собраны только последние ролики — полную историю TikTok не отдал."))
    except Exception as e:
        await update.callback_query.message.reply_text(f"⚠️ Не вышло: {e}")


# ─── Правила ──────────────────────────────────────────────────────────────────

def rules_kb(telegram_id: int) -> InlineKeyboardMarkup:
    rows = []
    for r in db.get_rules(telegram_id):
        mark = "✅" if r["enabled"] else "⏸"
        rows.append([InlineKeyboardButton(
            f"{mark} {r['source_name']} → {r['group_name']} · {r['videos_per_day']}/день",
            callback_data=f"rd:{r['id']}")])
    rows.append([InlineKeyboardButton("➕ Добавить правило", callback_data="r_add")])
    return InlineKeyboardMarkup(rows)


def rule_detail_text(r) -> str:
    lo, hi = r["min_duration"], r["max_duration"]
    dur = "без фильтра" if lo is None and hi is None else f"{lo if lo is not None else '—'}…{hi if hi is not None else '—'} с"
    return (
        f"📋 Правило #{r['id']}\n"
        f"Источник: {r['source_name']}\n"
        f"Группа: {r['group_name']} (id {r['vk_group_id']})\n"
        f"🎞 Видео в день: {r['videos_per_day']}\n"
        f"📝 Описание: {(r['description'] or '—')[:100]}\n"
        f"⏱ Длительность: {dur}\n"
        f"↕️ Порядок: {'сначала старые' if r['order_dir'] == 'old' else 'сначала новые'}\n"
        f"🔁 Перезаливка: {'вкл' if r['allow_repost'] else 'выкл'}\n"
        f"Статус: {'включено' if r['enabled'] else 'выключено'}"
    )


def rule_detail_kb(r) -> InlineKeyboardMarkup:
    rid = r["id"]
    return _kb([
        [("🎞 Видео/день", f"re:nday:{rid}"), ("📝 Описание", f"re:desc:{rid}")],
        [("⏱ Длительность", f"re:dur:{rid}"), ("↕️ Порядок", f"ro:{rid}")],
        [("🔁 Перезаливка", f"rp:{rid}"), ("⏸ Выкл" if r["enabled"] else "▶️ Вкл", f"rt:{rid}")],
        [("🗑 Удалить", f"rx:{rid}"), ("⬅️ К списку", "r_list")],
    ])


def _own_rule(update: Update, rid: int):
    r = db.get_rule(rid)
    return r if r and r["telegram_id"] == update.effective_user.id else None


async def show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show(update, "Твои правила (источник → группа):", rules_kb(update.effective_user.id))


async def r_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    await show_rules(update, context)


async def r_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    r = _own_rule(update, int(update.callback_query.data.split(":")[1]))
    if not r:
        await update.callback_query.answer("Правило не найдено")
        return
    await update.callback_query.answer()
    await _show(update, rule_detail_text(r), rule_detail_kb(r))


async def r_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    action, rid = update.callback_query.data.split(":")
    r = _own_rule(update, int(rid))
    if not r:
        await update.callback_query.answer("Правило не найдено")
        return
    await update.callback_query.answer()
    if action == "rx":
        db.delete_rule(r["id"])
        await show_rules(update, context)
        return
    if action == "rt":
        db.update_rule(r["id"], enabled=0 if r["enabled"] else 1)
    elif action == "rp":
        db.update_rule(r["id"], allow_repost=0 if r["allow_repost"] else 1)
    elif action == "ro":
        db.update_rule(r["id"], order_dir="new" if r["order_dir"] == "old" else "old")
    r = db.get_rule(r["id"])
    await _show(update, rule_detail_text(r), rule_detail_kb(r))


async def r_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    sources, groups = db.get_sources(uid), db.get_groups(uid)
    if not sources or not groups:
        await _show(update, "Сначала добавь хотя бы одну группу и один источник.")
        return ConversationHandler.END
    await _show(update, "Выбери источник:",
                _kb([[(s["name"], f"r_src:{s['id']}")] for s in sources]))
    return R_SRC


async def r_src(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["r_src"] = int(update.callback_query.data.split(":")[1])
    groups = db.get_groups(update.effective_user.id)
    await _show(update, "Выбери группу:",
                _kb([[(f"{g['name']} (id {g['vk_group_id']})", f"r_grp:{g['id']}")] for g in groups]))
    return R_GRP


async def r_grp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["r_grp"] = int(update.callback_query.data.split(":")[1])
    await _show(update, "Сколько видео в день выдавать для этой группы? (число)")
    return R_N


async def r_n(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 100:
        await update.message.reply_text("Нужно число от 1 до 100.")
        return R_N
    try:
        rid = db.add_rule(update.effective_user.id, context.user_data.pop("r_src"),
                          context.user_data.pop("r_grp"), videos_per_day=int(raw))
    except Exception as e:
        await update.message.reply_text(f"❌ Не вышло (такая пара уже есть?): {e}", reply_markup=MENU)
        return ConversationHandler.END
    r = db.get_rule(rid)
    await update.message.reply_text("✅ Правило создано.\n\n" + rule_detail_text(r),
                                    reply_markup=rule_detail_kb(r))
    return ConversationHandler.END


_RULE_FIELD_PROMPTS = {
    "nday": "Сколько видео в день выдавать? Пришли число (например 3):",
    "desc": "Пришли текст описания (или «-», чтобы убрать):",
    "dur": ("Фильтр по длительности в секундах. Пришли «min max» (например «30 60»).\n"
            "Только минимум: «30 -». Только максимум: «- 60». Убрать фильтр: «-»."),
}


def _parse_duration_input(text: str) -> tuple[int | None, int | None, str | None]:
    if text == "-":
        return None, None, None
    parts = text.split()
    if len(parts) != 2:
        return None, None, "Формат: «min max», например «30 60» (или «-» чтобы убрать)."

    def one(tok):
        if tok == "-":
            return None, False
        return (int(tok), False) if tok.isdigit() else (None, True)

    lo, e1 = one(parts[0])
    hi, e2 = one(parts[1])
    if e1 or e2:
        return None, None, "Границы должны быть числами или «-»."
    if lo is not None and hi is not None and lo > hi:
        return None, None, "Минимум больше максимума."
    return lo, hi, None


def desc_picker_kb(telegram_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"📝 {t['title']}", callback_data=f"rtpl:{t['id']}")]
            for t in db.get_templates(telegram_id)]
    rows.append([InlineKeyboardButton("✏️ Ввести вручную", callback_data="rdesc_manual"),
                 InlineKeyboardButton("🗑 Очистить", callback_data="rdesc_clear")])
    return InlineKeyboardMarkup(rows)


async def r_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _, field, rid = update.callback_query.data.split(":")
    r = _own_rule(update, int(rid))
    if not r:
        await update.callback_query.answer("Правило не найдено")
        return ConversationHandler.END
    await update.callback_query.answer()
    context.user_data["r_edit_id"] = r["id"]
    context.user_data["r_edit_field"] = field
    if field == "desc" and db.get_templates(update.effective_user.id):
        await _show(update, "Выбери заготовку или введи описание вручную:",
                    desc_picker_kb(update.effective_user.id))
        return R_DESC_PICK
    await _show(update, _RULE_FIELD_PROMPTS[field])
    return R_EDIT_VALUE


async def _finish_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, rid: int) -> int:
    context.user_data.pop("r_edit_id", None)
    context.user_data.pop("r_edit_field", None)
    r = db.get_rule(rid)
    await _show(update, rule_detail_text(r), rule_detail_kb(r))
    return ConversationHandler.END


async def r_desc_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    rid = context.user_data.get("r_edit_id")
    if rid is None:
        return ConversationHandler.END
    if q.data == "rdesc_manual":
        await _show(update, _RULE_FIELD_PROMPTS["desc"])
        return R_EDIT_VALUE
    if q.data == "rdesc_clear":
        db.update_rule(rid, description=None)
    elif q.data.startswith("rtpl:"):
        t = db.get_template(int(q.data.split(":")[1]))
        if t and t["telegram_id"] == update.effective_user.id:
            db.update_rule(rid, description=t["body"])
    return await _finish_edit(update, context, rid)


async def r_edit_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    rid = context.user_data.get("r_edit_id")
    field = context.user_data.get("r_edit_field")
    text = update.message.text.strip()
    if rid is None or field is None:
        await update.message.reply_text("Сессия истекла, открой правило заново.")
        return ConversationHandler.END
    error = None
    if field == "nday":
        if not text.isdigit() or not 1 <= int(text) <= 100:
            error = "Нужно целое число от 1 до 100."
        else:
            db.update_rule(rid, videos_per_day=int(text))
    elif field == "desc":
        db.update_rule(rid, description=None if text == "-" else update.message.text)
    elif field == "dur":
        lo, hi, err = _parse_duration_input(text)
        if err:
            error = err
        else:
            db.update_rule(rid, min_duration=lo, max_duration=hi)
    if error:
        await update.message.reply_text(f"❌ {error} Попробуй ещё раз или /cancel.")
        return R_EDIT_VALUE
    return await _finish_edit(update, context, rid)


# ─── Меню ─────────────────────────────────────────────────────────────────────

MENU_ROUTES = {
    BTN_GROUPS: show_groups, BTN_SOURCES: show_sources, BTN_RULES: show_rules,
    BTN_STATUS: cmd_status, BTN_PLAN: cmd_plan, BTN_TEMPLATES: show_templates,
}


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await MENU_ROUTES[update.message.text](update, context)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Необработанная ошибка", exc_info=context.error)


def build_app() -> Application:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit("Задай TELEGRAM_TOKEN в .env")
    db.init_db()

    builder = Application.builder().token(token).post_init(scheduler.restore_and_schedule)
    proxy = os.getenv("TELEGRAM_PROXY")
    if proxy:
        # Только для запросов к Telegram; скачивание видео ходит через DOWNLOAD_PROXY.
        builder = (
            builder.proxy(proxy).get_updates_proxy(proxy)
            .connect_timeout(20).read_timeout(30).write_timeout(30)
            .get_updates_connect_timeout(20).get_updates_read_timeout(30)
        )
    app = builder.build()
    app.add_handler(TypeHandler(Update, gate), group=-1)

    text = filters.TEXT & ~filters.COMMAND
    menu_filter = filters.Text(list(MENU_TEXTS))
    plain = text & ~menu_filter
    fallbacks = [CommandHandler("cancel", cancel), MessageHandler(menu_filter, menu_escape)]

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(g_add, pattern=r"^g_add$"),
            CallbackQueryHandler(g_ren, pattern=r"^g_ren:\d+$"),
            CallbackQueryHandler(t_add, pattern=r"^t_add$"),
            CallbackQueryHandler(t_edit, pattern=r"^t_edit:\d+$"),
            CallbackQueryHandler(s_add_acc, pattern=r"^s_add_acc$"),
            CallbackQueryHandler(s_add_db, pattern=r"^s_add_db$"),
            CallbackQueryHandler(r_add, pattern=r"^r_add$"),
            CallbackQueryHandler(r_edit_start, pattern=r"^re:(nday|desc|dur):\d+$"),
        ],
        states={
            G_LINK: [MessageHandler(plain, g_link)],
            G_CONFIRM: [CallbackQueryHandler(g_confirm, pattern=r"^g_(ok|own)$")],
            G_NAME: [MessageHandler(plain, g_name)],
            G_RENAME: [MessageHandler(plain, g_rename_save)],
            T_TITLE: [MessageHandler(plain, t_title)],
            T_BODY: [MessageHandler(plain, t_body)],
            S_NICK: [MessageHandler(plain, s_nick)],
            S_PATH: [MessageHandler(plain, s_path)],
            S_USER: [MessageHandler(plain, s_user)],
            R_SRC: [CallbackQueryHandler(r_src, pattern=r"^r_src:\d+$")],
            R_GRP: [CallbackQueryHandler(r_grp, pattern=r"^r_grp:\d+$")],
            R_N: [MessageHandler(plain, r_n)],
            R_EDIT_VALUE: [MessageHandler(plain, r_edit_save)],
            R_DESC_PICK: [CallbackQueryHandler(r_desc_pick, pattern=r"^(rtpl:\d+|rdesc_manual|rdesc_clear)$")],
        },
        fallbacks=fallbacks,
        allow_reentry=True,
    )
    app.add_handler(conv)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("testplan", cmd_testplan))
    app.add_handler(CommandHandler("errors", cmd_errors))
    app.add_handler(CommandHandler("templates", show_templates))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("startBot", cmd_start_bot))
    app.add_handler(CommandHandler("process", cmd_process))
    app.add_handler(CallbackQueryHandler(g_del, pattern=r"^g_del:\d+$"))
    app.add_handler(CallbackQueryHandler(t_del, pattern=r"^t_del:\d+$"))
    app.add_handler(CallbackQueryHandler(s_del, pattern=r"^s_del:\d+$"))
    app.add_handler(CallbackQueryHandler(s_ref, pattern=r"^s_ref:\d+$"))
    app.add_handler(CallbackQueryHandler(s_all, pattern=r"^s_all:\d+$"))
    app.add_handler(CallbackQueryHandler(r_detail, pattern=r"^rd:\d+$"))
    app.add_handler(CallbackQueryHandler(r_list, pattern=r"^r_list$"))
    app.add_handler(CallbackQueryHandler(r_action, pattern=r"^r[topx]:\d+$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern=r"^noop$"))
    app.add_handler(MessageHandler(menu_filter, menu_router))
    app.add_error_handler(on_error)

    scheduler.register_jobs(app)
    if config.API_TOKEN:
        import api
        api.start_in_thread(config.API_HOST, config.API_PORT, config.API_TOKEN, config.VIDEO_DIR)
    return app


def main() -> None:
    build_app().run_polling()


if __name__ == "__main__":
    main()
