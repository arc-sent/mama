"""Сборка JSON-плана выдачи. Чистая функция — без БД и сети.

Формат (то, что бот присылает в Telegram): аккаунт → группы → видео.
    {"id": 1, "accounts": [{"id": 1234567, "profile": "682d1e9fb6f4", "groups": [
        {"id": 2343, "videos": [{"id": 1, "path": "/c/video/video1.mp4"}]}]}]}

``accounts[].id`` — id страницы VK (vk.com/id1234567), ``profile`` — профиль ManyFaces на ПК, вошедший
в эту страницу (null — профиль по умолчанию). Группы без аккаунта собираются в аккаунт с
``"id": null, "profile": null``.

С ``extended=True`` (выгрузка для ПК) к аккаунтам добавляются ``name`` и ``profile_name``, к группам —
``name``, а к видео — ``description`` из правила или подпись из источника.
"""

import os


def plan_groups(plan: dict) -> list[dict]:
    """Все группы плана (сами словари, не копии): из ``accounts[].groups`` или прежнего ``groups``."""
    if isinstance(plan.get("accounts"), list):
        return [g for a in plan["accounts"] for g in a.get("groups") or []]
    return plan.get("groups") or []


def build_plan(plan_id: int, items, path_prefix: str, *, extended: bool = False) -> dict:
    """items — строки очереди (dict-подобные) с ключами id, vk_group_id, file_path, account_vk_id, profile_id
    (и group_name, account_name, profile_name, rule_description, video_description для extended).

    Аккаунты и группы идут в порядке первого появления, видео внутри группы — по id очереди.
    """
    prefix = path_prefix.rstrip("/")
    accounts: dict = {}
    for it in sorted(items, key=lambda r: r["id"]):
        video = {"id": it["id"], "path": f"{prefix}/{os.path.basename(it['file_path'])}"}
        if extended:
            # описание, заданное в правиле, главнее; иначе подпись самого видео из источника
            description = it["rule_description"] or it["video_description"]
            if description:
                video["description"] = description
        vk_id = _get(it, "account_vk_id")
        acc = accounts.get(vk_id)
        if acc is None:
            acc = accounts[vk_id] = {"id": vk_id, "profile": _get(it, "profile_id") if vk_id else None,
                                     "groups": {}}
            if extended:
                acc["name"] = _get(it, "account_name")
                acc["profile_name"] = _get(it, "profile_name") if vk_id else None
        group = acc["groups"].get(it["vk_group_id"])
        if group is None:
            group = acc["groups"][it["vk_group_id"]] = {"id": it["vk_group_id"], "videos": []}
            if extended:
                group = acc["groups"][it["vk_group_id"]] = {"id": it["vk_group_id"], "name": it["group_name"],
                                                            "videos": []}
        group["videos"].append(video)
    return {"id": plan_id,
            "accounts": [{**a, "groups": list(a["groups"].values())} for a in accounts.values()]}


def _get(row, key):
    """Поле строки очереди, которого может не быть (старые тесты, пример плана)."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None
