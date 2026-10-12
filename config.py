"""Настройки mother из окружения (.env подхватывает bot.py через python-dotenv)."""

import os

import pytz

MOSCOW_TZ = pytz.timezone("Europe/Moscow")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Куда физически кладём подготовленные видео и как их пути выглядят в JSON.
VIDEO_DIR = os.getenv("VIDEO_DIR", os.path.join(BASE_DIR, "data", "videos"))
OUTPUT_PATH_PREFIX = os.getenv("OUTPUT_PATH_PREFIX", "/c/video").rstrip("/")

# Когда готовим видео и когда отдаём JSON-план (МСК).
PREPARE_HOUR = int(os.getenv("PREPARE_HOUR", "8"))
PREPARE_MINUTE = int(os.getenv("PREPARE_MINUTE", "0"))
DELIVER_HOUR = int(os.getenv("DELIVER_HOUR", "12"))
DELIVER_MINUTE = int(os.getenv("DELIVER_MINUTE", "0"))

# Автодопарсинг истории аккаунтов.
BACKLOG_DAYS = int(os.getenv("BACKLOG_DAYS", "3"))
DEEPEN_MAX_PASSES = int(os.getenv("DEEPEN_MAX_PASSES", "3"))

# Сколько раундов «спланировать → скачать» делаем за подготовку, если часть
# видео не скачалась (битая ссылка, слишком длинное и т.п.).
PREPARE_ROUNDS = int(os.getenv("PREPARE_ROUNDS", "3"))

# API для ПК с браузерными профилями: отдаёт план и видео, принимает результат выкладки.
# Включается, если задан MOTHER_API_TOKEN. По умолчанию слушает только свой компьютер;
# наружу его выставляют через HTTPS-прокси или закрытую сеть (Tailscale/WireGuard).
API_TOKEN = os.getenv("MOTHER_API_TOKEN", "")
API_HOST = os.getenv("MOTHER_API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("MOTHER_API_PORT", "8787"))
# Через сколько дней после ответа ПК удалять видео плана с диска сервера.
KEEP_DAYS = int(os.getenv("KEEP_DAYS", "2"))

VIDEO_PROCESS_CONCURRENCY = int(os.getenv("VIDEO_PROCESS_CONCURRENCY", "1"))

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
