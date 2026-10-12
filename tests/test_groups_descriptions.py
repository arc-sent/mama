import pytest

import db
import vk_link


@pytest.mark.parametrize("text,expected", [
    ("vk.com/club123456", 123456),
    ("https://vk.com/public22822305", 22822305),
    ("https://m.vk.com/event77?from=feed", 77),
    ("vk.com/video-555_12", 555),
    ("https://vk.com/wall-42_7", 42),
    ("-2343", 2343),
    ("2343", 2343),
])
def test_resolve_group_locally(text, expected, monkeypatch):
    monkeypatch.setattr(vk_link, "fetch_by_screen_name", lambda n: (None, "Название"))
    gid, name, err = vk_link.resolve_group(text)
    assert (gid, name, err) == (expected, "Название", None)


def test_short_name_without_page_is_error(monkeypatch):
    monkeypatch.setattr(vk_link, "fetch_by_screen_name", lambda n: (None, None))
    gid, name, err = vk_link.resolve_group("vk.com/some_short_name")
    assert gid is None and err


def test_short_name_uses_page_lookup(monkeypatch):
    monkeypatch.setattr(vk_link, "fetch_by_screen_name", lambda n: (99, "Группа"))
    assert vk_link.resolve_group("https://vk.com/my_group") == (99, "Группа", None)


def test_rule_description_and_templates(user_group_source):
    ctx = user_group_source
    rid = db.add_rule(ctx["telegram_id"], ctx["source_id"], ctx["group_id"], description="привет")
    assert db.get_rule(rid)["description"] == "привет"
    db.update_rule(rid, description=None)
    assert db.get_rule(rid)["description"] is None

    db.add_template(ctx["telegram_id"], "Заг", "Текст #tag")
    t = db.get_templates(ctx["telegram_id"])[0]
    db.update_template(t["id"], "Заг2", "Новый")
    assert db.get_template(t["id"])["body"] == "Новый"
    db.delete_template(t["id"])
    assert db.get_templates(ctx["telegram_id"]) == []


def test_embedded_group_name_parsed_from_page():
    html = 'x"version":"5.289","response":{"groups":[{"id":5,"name":"\u041a\u0438\u0442","a":1}]},"y"'
    assert vk_link._embedded_group_name(html, 5) == "Кит"
    assert vk_link._embedded_group_name(html, 6) is None
    assert vk_link._embedded_group_name("no groups here", 5) is None
