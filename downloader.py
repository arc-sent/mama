"""Скачивание видео с TikTok, Likee и YouTube Shorts."""

import asyncio
import logging
import os
import re
import ssl
import subprocess
import tempfile
import uuid

import urllib3
import yt_dlp
import requests
from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

urllib3.disable_warnings()
logger = logging.getLogger(__name__)

MAX_VIDEO_DURATION = 180  # секунд (3 минуты)

# Прокси для скачивания видео (все платформы). Формат: http://user:pass@host:port
DOWNLOAD_PROXY: str | None = os.getenv("DOWNLOAD_PROXY") or None


# ─── HTTP сессия с ослабленным SSL ───────────────────────────────────────────

class _PermissiveSSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT@SECLEVEL=0")
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **kwargs):
        ctx = create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT@SECLEVEL=0")
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kwargs["ssl_context"] = ctx
        return super().proxy_manager_for(proxy, **kwargs)


def _make_session() -> Session:
    s = Session()
    adapter = _PermissiveSSLAdapter()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _tmp_path(prefix: str) -> str:
    d = os.path.join(tempfile.gettempdir(), "mother_bot")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{prefix}_{uuid.uuid4().hex}")


# ─── Определение платформы ────────────────────────────────────────────────────

_TIKTOK_RE  = re.compile(r"tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com", re.IGNORECASE)
_LIKEE_RE   = re.compile(r"https?://(?:l\.)?likee\.video/", re.IGNORECASE)
_YOUTUBE_RE = re.compile(
    r"https?://(?:(?:www|m)\.)?(?:youtube\.com/(?:shorts/|watch\?|embed/|v/|live/)|youtu\.be/)",
    re.IGNORECASE,
)


def detect_platform(url: str) -> str | None:
    """Возвращает 'tiktok', 'likee', 'youtube' или None."""
    if _TIKTOK_RE.search(url):
        return "tiktok"
    if _LIKEE_RE.match(url):
        return "likee"
    if _YOUTUBE_RE.match(url):
        return "youtube"
    return None


# ─── Универсальный загрузчик через yt-dlp (TikTok, YouTube Shorts) ────────────

def _download_ytdlp_sync(
    url: str,
    save_path: str | None,
    prefix: str,
    default_title: str,
    proxy: str | None = None,
) -> tuple[str, str]:
    _proxy = proxy or DOWNLOAD_PROXY
    meta_opts: dict = {"quiet": True, "no_warnings": True}
    if _proxy:
        meta_opts["proxy"] = _proxy

    # Фаза 1: получаем метаданные без скачивания — проверяем длительность
    with yt_dlp.YoutubeDL(meta_opts) as ydl:
        meta = ydl.extract_info(url, download=False)
    _check_duration(meta.get("duration"))

    # Фаза 2: скачиваем
    tmpdir = save_path or _tmp_path(f"{prefix}_dir")
    os.makedirs(tmpdir, exist_ok=True)

    ydl_opts = {
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        # Приоритет H.264: клипам нужен H.264, а источники отдают
        # высокое разрешение в HEVC/VP9. Берём лучший доступный H.264-вариант.
        "format_sort": ["vcodec:h264"],
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        # Отдельный кэш-каталог на каждый вызов — несколько параллельных
        # загрузок не будут конкурировать за один и тот же кэш-файл.
        "cachedir": os.path.join(tmpdir, ".cache"),
    }
    if _proxy:
        ydl_opts["proxy"] = _proxy
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        expected = ydl.prepare_filename(info)

    title = (info.get("title") or default_title)[:100]
    # Берём именно тот файл, который yt-dlp считает итоговым, а не первый
    # попавшийся — это исключает выбор промежуточных .part-файлов.
    candidate = expected if os.path.isfile(expected) else None
    if candidate is None:
        mp4s = sorted(
            (f for f in os.listdir(tmpdir) if f.endswith(".mp4") and os.path.isfile(os.path.join(tmpdir, f))),
            key=lambda f: os.path.getsize(os.path.join(tmpdir, f)),
            reverse=True,
        )
        if not mp4s:
            raise RuntimeError(f"Файл ({prefix}) не был скачан")
        candidate = os.path.join(tmpdir, mp4s[0])
    return _ensure_h264(candidate), title


def _download_tiktok_sync(url: str, save_path: str | None) -> tuple[str, str]:
    try:
        return _download_ytdlp_sync(url, save_path, "tiktok", "TikTok Video", proxy=None)
    except ValueError:
        raise  # видео слишком длинное — прокси не поможет
    except Exception as first_err:
        if not DOWNLOAD_PROXY:
            raise
        logger.warning("TikTok: прямое скачивание не удалось (%s) — повтор через прокси", first_err)
        return _download_ytdlp_sync(url, save_path, "tiktok", "TikTok Video", proxy=DOWNLOAD_PROXY)


async def download_tiktok(url: str, save_path: str | None = None) -> tuple[str, str]:
    """Возвращает (путь к файлу, название)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download_tiktok_sync, url, save_path)


async def download_youtube(url: str, save_path: str | None = None) -> tuple[str, str]:
    """Скачивает YouTube Shorts (и обычные видео) через yt-dlp.

    Возвращает (путь к файлу, название)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _download_ytdlp_sync, url, save_path, "youtube", "YouTube Video"
    )


# ─── Likee ────────────────────────────────────────────────────────────────────

_LIKEE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/20A362"
    ),
    "Referer": "https://likee.video/",
}


def _meta(html: str, prop: str) -> str:
    m = re.search(
        r'<meta[^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\']',
        html, re.IGNORECASE,
    )
    return m.group(1) if m else ""


def _get_likee_info_sync(url: str) -> dict:
    resp = _make_session().get(url, headers=_LIKEE_HEADERS, allow_redirects=True, proxies=None, timeout=15)
    html = resp.text

    video_url = _meta(html, "og:video:secure_url") or _meta(html, "og:video")
    if not video_url:
        raise ValueError("Не удалось найти видео на странице Likee. Видео может быть удалено или недоступно.")

    post_id = ""
    m = re.search(r'postid=(\d+)', html, re.IGNORECASE) or re.search(r'/video/(\d+)', resp.url)
    if m:
        post_id = m.group(1)

    title = _meta(html, "og:title") or post_id or "Likee Video"
    duration_str = _meta(html, "video:duration") or _meta(html, "og:video:duration")
    duration = int(duration_str) if duration_str and duration_str.isdigit() else None
    return {"video_url": video_url, "title": title, "duration": duration}


def _download_likee_sync(url: str) -> tuple[str, str]:
    info = _get_likee_info_sync(url)
    _check_duration(info.get("duration"))
    out_path = _tmp_path("likee") + ".mp4"

    with _make_session().get(
        info["video_url"], headers=_LIKEE_HEADERS, stream=True, proxies=None, timeout=120,
    ) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)

    return _ensure_h264(out_path), info["title"]


async def download_likee(url: str) -> tuple[str, str]:
    """Возвращает (путь к файлу, название)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download_likee_sync, url)


def _check_duration(duration: int | None) -> None:
    """Бросает ValueError если длительность превышает лимит."""
    if duration and duration > MAX_VIDEO_DURATION:
        mins, secs = divmod(duration, 60)
        raise ValueError(f"Видео слишком длинное — {mins}:{secs:02d}. Максимум 3 минуты.")


def _ensure_h264(path: str) -> str:
    """Гарантирует, что видеопоток ролика — H.264.

    VK помещает запись в раздел «Клипы», только если видео в кодеке H.264.
    Ролики в HEVC (H.265) — например, высокое разрешение с TikTok — VK кладёт
    в обычные «Видео». Мы всегда стараемся скачать сразу H.264 (см. format_sort),
    но если у источника есть только HEVC/иной кодек — перекодируем, сохраняя
    разрешение. Возвращает путь к H.264-файлу (тот же или новый).
    """
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60,
        )
        codec = probe.stdout.strip().lower()
    except Exception:
        logger.exception("ffprobe не сработал — оставляю файл как есть")
        return path

    if codec in ("h264", "avc1", ""):
        return path  # уже H.264 (либо не смогли определить — не трогаем)

    logger.info("видеокодек %s — перекодирую в H.264 для совместимости с клипами VK", codec)
    out_path = path + ".h264.mp4"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", path,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", out_path],
            capture_output=True, timeout=600, check=True,
        )
    except Exception:
        logger.exception("перекодирование в H.264 не удалось — публикую исходный файл")
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass
        return path

    try:
        os.remove(path)
    except OSError:
        pass
    return out_path
