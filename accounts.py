"""Аккаунты: страница VK + профиль ManyFaces на ПК, вошедший в неё. Без сети и Telegram — только база.

Страницу VK бот проверяет по ссылке (``vk_link.resolve_user``), как группу. Профили ManyFaces живут на
ПК, сервер их не видит: ПК присылает их список (``POST /profiles``, это делает
``pc_agent/pull_plan.py`` при каждом запуске и командой ``profiles``), и профиль аккаунта выбирают
кнопкой из этого списка.
"""

from datetime import datetime

import db
from config import MOSCOW_TZ

DEFAULT_PROFILE = "-"            # в callback: «профиль по умолчанию на ПК»


def synced_text() -> str:
    ts = db.pc_profiles_synced_at()
    if not ts:
        return "ПК ещё ни разу не присылал список профилей ManyFaces."
    return f"Список профилей ManyFaces с ПК от {datetime.fromtimestamp(ts, MOSCOW_TZ):%d.%m %H:%M} МСК."


def known_profiles_text(limit: int = 30) -> str:
    rows = db.get_pc_profiles()
    if not rows:
        return synced_text()
    lines = [f"• {r['name']} ({r['profile_id']})" for r in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"…и ещё {len(rows) - limit}")
    return synced_text() + "\n" + "\n".join(lines)


HOW_TO_SYNC = ("Если нужного профиля нет в списке, обнови его на ПК:\n"
               "python pc_agent\\pull_plan.py profiles\n"
               "(он и так обновляется при каждом запуске выкладки).")


def profile_choices() -> list[tuple[str, str]]:
    """[(текст кнопки, значение)] для выбора профиля: профили с ПК и «по умолчанию»."""
    rows = [(f"🖥 {r['name']} ({r['profile_id']})", r["profile_id"]) for r in db.get_pc_profiles()]
    rows.append(("— профиль по умолчанию на ПК", DEFAULT_PROFILE))
    return rows


def pick_profile(value: str) -> tuple[bool, str | None, str | None]:
    """(годится, id профиля, название) по значению кнопки. Профиль должен быть в списке с ПК."""
    if value == DEFAULT_PROFILE:
        return True, None, None
    row = db.get_pc_profile(value)
    if row is None:
        return False, None, None
    return True, row["profile_id"], row["name"]


def account_line(a) -> str:
    profile = f"🖥 {a['profile_name'] or a['profile_id']}" if a["profile_id"] else "🖥 по умолчанию"
    return f"🧑‍💻 {a['name']} (vk id {a['vk_user_id']}) → {profile}"
