"""Выбранный аккаунт (как в omega) и листание списков стрелками."""

import os

os.environ.setdefault("TELEGRAM_TOKEN", "test")

import bot  # noqa: E402
import db  # noqa: E402

UID = 1001


def _setup():
    db.ensure_user(UID)
    a1 = db.add_account(UID, 111, "Алексей", "p1", "Профиль 1")
    a2 = db.add_account(UID, 222, "Второй", None, None)
    g1 = db.add_group(UID, 1, "Г1")
    g2 = db.add_group(UID, 2, "Г2")
    g3 = db.add_group(UID, 3, "Г3")                      # без аккаунта
    db.set_group_account(g1, a1)
    db.set_group_account(g2, a2)
    s_shared = db.add_source(UID, "общий", "/x.db", None)
    s_a1 = db.add_source(UID, "свой", "/y.db", None, owner_account_id=a1)
    db.add_rule(UID, s_shared, g1)
    db.add_rule(UID, s_shared, g2)
    db.add_rule(UID, s_a1, g3)
    return a1, a2, (g1, g2, g3), (s_shared, s_a1)


def _ids(rows):
    return [r["id"] for r in rows]


def test_lists_are_filtered_by_the_chosen_account():
    a1, a2, (g1, g2, g3), (s_shared, s_a1) = _setup()
    assert _ids(db.get_groups(UID, a1)) == [g1]
    assert _ids(db.get_groups(UID, 0)) == [g3]                       # «📂 Без аккаунта»
    assert _ids(db.get_groups(UID)) == [g1, g2, g3]                  # без отбора — всё
    assert _ids(db.get_sources(UID, a1)) == [s_a1]                   # у аккаунта только свои
    assert _ids(db.get_sources(UID, a2)) == []
    assert _ids(db.get_sources(UID, 0)) == [s_shared]                # без владельца — «без аккаунта»
    assert [r["group_id"] for r in db.get_rules(UID, a2)] == [g2]
    assert [r["group_id"] for r in db.get_rules(UID, 0)] == [g3]


def test_current_account_defaults_to_the_first_and_survives_deletion():
    assert bot.current_scope(UID) is None                            # аккаунтов нет — показывается всё
    a1, a2, *_ = _setup()
    assert bot.current_scope(UID) == a1                              # не выбран — первый
    db.set_current_account(UID, a2)
    assert bot.current_scope(UID) == a2
    db.set_current_account(UID, 0)
    assert bot.current_scope(UID) == 0                               # «без аккаунта» — тоже выбор
    db.set_current_account(UID, a2)
    db.delete_account(a2)
    assert bot.current_scope(UID) == a1                              # удалённый — снова первый


def test_accounts_keyboard_marks_the_chosen_one_and_offers_to_switch():
    a1, a2, *_ = _setup()
    kb = bot.accounts_kb(UID)
    texts = [b.text for row in kb.inline_keyboard for b in row]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(t.startswith("✅ ") and "Алексей" in t for t in texts)
    assert f"a_sw:{a2}" in datas and f"a_sw:{a1}" not in datas       # «→ Перейти» только у невыбранных
    assert "a_sw:0" in datas                                         # раздел «📂 Без аккаунта»


def test_long_lists_get_arrows_instead_of_endless_buttons():
    db.ensure_user(UID)
    for i in range(1, 14):
        db.add_group(UID, 1000 + i, f"Группа {i}")
    kb = bot.groups_kb(UID)                                          # аккаунтов нет — все 13 групп
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert datas.count("noop") >= bot.PAGE_GROUPS                    # 6 групп на странице
    assert "g_pg:1" in datas and "g_pg:-1" not in datas              # только «➡️» на первой
    last = [b.callback_data for row in bot.groups_kb(UID, 99).inline_keyboard for b in row]
    assert "g_pg:1" in last and "g_pg:3" not in last                 # последняя (3/3): только «⬅️»
    for i in range(1, 9):
        db.add_source(UID, f"src{i}", f"/{i}.db", None)
    _, skb = bot.sources_view(UID)
    assert "s_pg:1" in [b.callback_data for row in skb.inline_keyboard for b in row]



def test_old_sources_are_given_to_the_accounts_of_their_rules():
    import sqlite3
    a1, a2, (g1, g2, g3), (s_shared, s_a1) = _setup()       # s_shared кормит группы a1, a2 и без аккаунта
    lonely = db.add_source(UID, "без правил", "/z.db", None)
    conn = sqlite3.connect(db.DB_PATH)                        # как в базе до появления владельцев
    conn.execute("UPDATE sources SET owner_account_id = NULL")
    conn.execute("DELETE FROM settings WHERE key = 'sources_owned_v1'")
    conn.commit(); conn.close()
    db.init_db()                                              # миграция при старте
    by_group = {r["group_id"]: r["source_id"] for r in db.get_rules(UID)}
    assert by_group[g1] == s_shared and db.get_source(s_shared)["owner_account_id"] == a1
    assert by_group[g2] != s_shared and db.get_source(by_group[g2])["owner_account_id"] == a2   # своя копия
    assert db.get_source(by_group[g2])["db_path"] == "/x.db"                                    # тот же файл
    assert db.get_source(by_group[g3])["owner_account_id"] is None and by_group[g3] == s_a1     # s_a1: только g3
    assert db.get_source(lonely)["owner_account_id"] is None                                    # без правил
    n = len(db.get_sources(UID))
    db.init_db()                                              # второй запуск ничего не меняет
    assert len(db.get_sources(UID)) == n
