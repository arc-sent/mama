"""Сборка JSON-плана выдачи. Чистая функция — без БД и сети.

Формат (то, что бот присылает в Telegram):
    {"id": 1, "groups": [{"id": 2343, "videos": [{"id": 1, "path": "/c/video/video1.mp4"}]}]}

С ``extended=True`` (выгрузка в файл для скрипта выкладки) к группам добавляется ``name``,
а к видео — ``description`` из правила, если оно задано.
"""

import os


def build_plan(plan_id: int, items, path_prefix: str, *, extended: bool = False) -> dict:
    """items — строки очереди (dict-подобные) с ключами id, vk_group_id, file_path
    (и group_name, description для extended).

    Группы идут в порядке первого появления, видео внутри группы — по id очереди.
    """
    prefix = path_prefix.rstrip("/")
    groups: dict[int, dict] = {}
    for it in sorted(items, key=lambda r: r["id"]):
        video = {"id": it["id"], "path": f"{prefix}/{os.path.basename(it['file_path'])}"}
        if extended:
            # описание, заданное в правиле, главнее; иначе подпись самого видео из источника
            description = it["rule_description"] or it["video_description"]
            if description:
                video["description"] = description
        group = groups.setdefault(it["vk_group_id"], {"id": it["vk_group_id"], "videos": []})
        if extended and "name" not in group:
            group = groups[it["vk_group_id"]] = {"id": group["id"], "name": it["group_name"],
                                                 "videos": group["videos"]}
        group["videos"].append(video)
    return {"id": plan_id, "groups": list(groups.values())}
