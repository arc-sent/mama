"""Определение id сообщества по ссылке — без токена и без VK API.

Форматы, которые понимаем локально: vk.com/club123, public123, event123,
video-123_456, wall-123_456, чистое число. Короткое имя (vk.com/my_group)
разрешаем по публичной странице сообщества: id лежит в её HTML.
"""

import json
import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

_HOST_RE = re.compile(r"^(?:https?://)?(?:(?:www|m)\.)?vk\.(?:com|ru)/", re.IGNORECASE)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def extract_screen_name(text: str) -> str:
    text = _HOST_RE.sub("", text.strip())
    text = text.split("?")[0].split("#")[0].strip("/")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text


def parse_local(raw: str) -> int | None:
    """id из club123 / public123 / event123 / video-123_4 / wall-123_4 / -123 / 123."""
    m = re.match(r"(?:video|wall|clip|photo)-(\d+)", raw, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.match(r"(?:club|public|event)(\d+)$", raw, re.IGNORECASE)
    if m:
        return int(m.group(1))
    if re.fullmatch(r"-?\d+", raw):
        return abs(int(raw))
    return None


def _lookup_proxies() -> dict | None:
    proxy = os.getenv("LINK_PROXY") or os.getenv("TELEGRAM_PROXY")
    return {"http": proxy, "https": proxy} if proxy else None


_ID_PATTERNS = (
    r'"group_ids"\s*:\s*\[?\s*(\d+)',
    r'"group_id"\s*:\s*"?(\d+)',
    r'rel="canonical"[^>]*href="https?://[^"]*/(?:club|public|event)(\d+)"',
    r'href="https?://[^"]*/(?:club|public|event)(\d+)"[^>]*rel="canonical"',
    r'"owner_id"\s*:\s*-(\d+)',
)


def _embedded_group_name(html: str, gid: int) -> str | None:
    """Страница сообщества несёт в себе ответ groups.getById — берём оттуда name."""
    i = html.find('"groups":[')
    if i < 0:
        return None
    try:
        groups, _ = json.JSONDecoder().raw_decode(html[i + len('"groups":'):])
    except ValueError:
        return None
    for g in groups if isinstance(groups, list) else []:
        if isinstance(g, dict) and g.get("id") == gid and g.get("name"):
            return str(g["name"])[:100]
    return None


def fetch_by_screen_name(name: str) -> tuple[int | None, str | None]:
    """(id, название) по короткому имени. (None, None), если не нашли; причина — в лог.

    Название VK без входа часто не отдаёт (общий заголовок) — тогда вернётся None
    и бот попросит ввести название вручную.
    """
    try:
        resp = requests.get(
            f"https://vk.com/{name}", headers={"User-Agent": _UA},
            timeout=20, proxies=_lookup_proxies(), allow_redirects=True,
        )
    except requests.RequestException as e:
        logger.warning("VK-ссылка %r: сеть: %r", name, e)
        return None, None
    if resp.status_code != 200:
        logger.warning("VK-ссылка %r: HTTP %s (%s)", name, resp.status_code, resp.url)
        return None, None
    html = resp.content.decode("cp1251", errors="replace")
    gid = None
    for pat in _ID_PATTERNS:
        m = re.search(pat, html)
        if m:
            gid = int(m.group(1))
            break
    if gid is None:
        logger.warning("VK-ссылка %r: id не найден на странице (%s, %s байт, редирект на %s)",
                       name, resp.status_code, len(html), resp.url)
        return None, None
    title = _embedded_group_name(html, gid)
    if title is None:
        t = re.search(r'property="og:title"\s+content="([^"]+)"', html)
        if t:
            cand = t.group(1).split("|")[0].strip()
            if cand and cand.lower() not in ("вконтакте", "vk"):
                title = cand[:100]
    return gid, title


def resolve_group(text: str) -> tuple[int | None, str | None, str | None]:
    """(id, название|None, ошибка|None). Название берём со страницы сообщества."""
    raw = extract_screen_name(text)
    if not raw:
        return None, None, "Пустая ссылка. Пришли ссылку на сообщество VK."
    gid = parse_local(raw)
    if gid:
        # id уже известен; название пробуем достать со страницы club<id>
        _, name = fetch_by_screen_name(f"club{gid}")
        return gid, name, None
    gid, name = fetch_by_screen_name(raw)
    if gid:
        return gid, name, None
    return None, None, (
        "Не смог определить сообщество по этой ссылке. Пришли ссылку вида "
        "vk.com/club123 или просто числовой id."
    )
