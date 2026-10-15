"""Аккаунты: страница VK (проверка по ссылке) + профиль ManyFaces из списка с ПК; привязка групп, план, API."""

import asyncio
import json
import threading
import urllib.error
import urllib.request

import pytest

import accounts
import api
import db
import notify
import scheduler
import vk_link
from test_scheduler import FakeBot, MORNING, _make_ready, _rule

TOKEN = "t" * 24
PC = [{"id": "682d1e9fb6f4", "name": "Профиль 1"}, {"id": "10c4c6d1771e", "name": "Тест"}]

USER_PAGE = ('...{"method":"users.get","request":{"user_ids":1},"version":"5.289","response":'
             '[{"id":1,"first_name":"Павел","last_name":"Дуров","is_closed":false}],"error":null}...')
DELETED_PAGE = ('{"method":"users.get","request":{},"response":[{"id":5,"first_name":"DELETED",'
                '"last_name":"","deactivated":"deleted"}]}')


# ---- страница VK --------------------------------------------------------------------------------

def test_user_page_is_resolved(monkeypatch):
    asked = []
    monkeypatch.setattr(vk_link, "_fetch_html", lambda name: asked.append(name) or USER_PAGE)
    assert vk_link.resolve_user("https://vk.com/durov") == (1, "Павел Дуров", None)
    assert vk_link.resolve_user("vk.com/id1")[0] == 1 and vk_link.resolve_user("1")[0] == 1
    assert asked == ["durov", "id1", "id1"]


def test_bad_user_pages_are_rejected(monkeypatch):
    monkeypatch.setattr(vk_link, "_fetch_html", lambda name: DELETED_PAGE)
    assert "удалена или заблокирована" in vk_link.resolve_user("id5")[2]
    monkeypatch.setattr(vk_link, "_fetch_html", lambda name: "<html>страница сообщества</html>")
    assert "Не нашёл страницу пользователя" in vk_link.resolve_user("vk.com/some_group")[2]
    monkeypatch.setattr(vk_link, "_fetch_html", lambda name: None)                # сеть / 404
    assert vk_link.resolve_user("vk.com/nobody")[0] is None
    assert "ссылка на сообщество" in vk_link.resolve_user("https://vk.com/club242283390")[2]


# ---- профиль ManyFaces --------------------------------------------------------------------------

def test_profile_is_picked_only_from_the_pc_list():
    assert accounts.profile_choices() == [("— профиль по умолчанию на ПК", "-")]
    assert "ни разу не присылал" in accounts.synced_text()
    db.set_pc_profiles(PC)
    assert [v for _, v in accounts.profile_choices()] == ["682d1e9fb6f4", "10c4c6d1771e", "-"]
    assert accounts.pick_profile("682d1e9fb6f4") == (True, "682d1e9fb6f4", "Профиль 1")
    assert accounts.pick_profile("-") == (True, None, None)
    assert accounts.pick_profile("aaaaaaaaaaaa")[0] is False                    # нет на ПК


def test_pc_list_is_replaced_not_merged():
    db.set_pc_profiles(PC)
    db.set_pc_profiles([{"id": "682d1e9fb6f4", "name": "Новое имя"}])
    assert [(r["profile_id"], r["name"]) for r in db.get_pc_profiles()] == [("682d1e9fb6f4", "Новое имя")]
    assert db.pc_profiles_synced_at()


# ---- аккаунты и группы --------------------------------------------------------------------------

def test_account_crud_and_group_link(user_group_source):
    uid, gid = user_group_source["telegram_id"], user_group_source["group_id"]
    aid = db.add_account(uid, 1, "Павел Дуров", "682d1e9fb6f4", "Профиль 1")
    assert db.add_account(uid, 1, "Переименован", "682d1e9fb6f4", "Профиль 1") == aid  # та же страница
    db.set_group_account(gid, aid)
    g = db.get_groups(uid)[0]
    assert g["account_name"] == "Переименован" and g["account_vk_id"] == 1 and g["profile_id"] == "682d1e9fb6f4"
    a = db.get_accounts(uid)[0]
    assert a["n_groups"] == 1
    assert accounts.account_line(a) == "🧑‍💻 Переименован (vk id 1) → 🖥 Профиль 1"
    db.set_account_profile(aid, None, None)
    assert accounts.account_line(db.get_accounts(uid)[0]).endswith("🖥 по умолчанию")

    db.delete_account(aid)                                                  # группа остаётся, без аккаунта
    g = db.get_groups(uid)[0]
    assert g["account_id"] is None and g["account_name"] is None
    assert db.get_accounts(uid) == []


def test_plan_is_split_by_account(user_group_source, tmp_path):
    uid, gid = user_group_source["telegram_id"], user_group_source["group_id"]
    aid = db.add_account(uid, 1, "Павел Дуров", "682d1e9fb6f4", "Профиль 1")
    db.set_group_account(gid, aid)
    rule = _rule(user_group_source, tmp_path, per_day=2)
    scheduler.plan_rule(rule, MORNING)
    _make_ready(tmp_path)

    bot = FakeBot()
    assert asyncio.run(scheduler.deliver_plan(bot, uid)) is True
    _, _, body = bot.documents[0]
    assert body["accounts"][0]["id"] == 1 and body["accounts"][0]["profile"] == "682d1e9fb6f4"
    assert body["accounts"][0]["groups"][0]["id"] == 2343
    ext = json.loads(db.get_plan(body["id"])["body"])                       # то, что получает ПК
    acc = ext["accounts"][0]
    assert acc["name"] == "Павел Дуров" and acc["profile_name"] == "Профиль 1"
    assert acc["groups"][0]["name"] == "Тестовая группа"


def test_notice_names_the_account():
    text = notify.format_result(9, {"groups": [
        {"id": 7, "name": "Котики", "account": {"id": 1, "name": "Павел Дуров"},
         "videos": [{"id": 1, "status": "ok"}]},
        {"id": 8, "name": "Собаки", "videos": [{"id": 2, "status": "failed", "error": "нет профиля"}]}]})
    assert "✅ Павел Дуров › Котики: видео 1" in text
    assert "❌ Собаки: видео 2 — нет профиля" in text


# ---- API ----------------------------------------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    srv = api.serve("127.0.0.1", 0, TOKEN, str(tmp_path))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _call(url, data=None, token=TOKEN):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Authorization": f"Bearer {token}",
                                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_api_receives_the_profiles(server):
    assert _call(server + "/profiles", {"profiles": PC}, token="wrong")[0] == 401
    assert _call(server + "/profiles", {"profiles": PC}) == (200, {"ok": True, "profiles": 2})
    status, got = _call(server + "/profiles")
    assert status == 200 and got["synced_at"] and {p["id"] for p in got["profiles"]} == {
        "682d1e9fb6f4", "10c4c6d1771e"}
    assert _call(server + "/profiles", {"profiles": [{"id": "../x"}]})[0] == 400
    assert _call(server + "/profiles", {"profiles": "x"})[0] == 400
