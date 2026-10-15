"""Файлы плана для передачи на другой компьютер: имя, размер и контрольная сумма каждого видео.

ПК забирает план по сети и скачивает видео по имени; сумма нужна, чтобы убедиться, что файл
дошёл целиком, и не качать то, что уже лежит."""

import hashlib
import os
import re

from plan_builder import plan_groups

# Имена, которые отдаёт API: только то, что создаёт сам mother (video<id>.mp4). Это и защита
# от выхода за пределы папки видео.
VIDEO_NAME_RE = re.compile(r"^video\d+\.mp4$")


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def add_file_info(plan: dict, items) -> dict:
    """Дописать каждому видео плана ``file``, ``size`` и ``sha256``.

    items — строки очереди, из которых собран план (по ним находится путь к файлу на диске).
    Видео, файла которого нет, остаётся без этих полей — агент увидит это и пропустит его."""
    by_id = {it["id"]: it["file_path"] for it in items}
    for group in plan_groups(plan):
        for video in group["videos"]:
            path = by_id.get(video["id"])
            if not path or not os.path.isfile(path):
                continue
            video["file"] = os.path.basename(path)
            video["size"] = os.path.getsize(path)
            video["sha256"] = sha256_of(path)
    return plan


def video_files(plan: dict) -> list[str]:
    return [v["file"] for g in plan_groups(plan) for v in g["videos"] if v.get("file")]


def safe_video_path(video_dir: str, name: str) -> str | None:
    """Путь к файлу в video_dir по имени из запроса; None, если имя не наше или файла нет."""
    if not VIDEO_NAME_RE.match(name or ""):
        return None
    path = os.path.join(video_dir, name)
    return path if os.path.isfile(path) else None
