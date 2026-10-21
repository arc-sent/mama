"""HTTP-API для ПК, который выкладывает видео. Только стандартная библиотека.

    GET  /health               — проверка (без токена)
    GET  /plan/latest          — самый свежий план без ответа ПК (404, если такого нет)
    GET  /plan/retry           — план-повтор (видео, которые в прошлый раз не вышли), ждущий ПК (404 — нет)
    GET  /plan/<id>            — конкретный план
    GET  /video/<video12.mp4>  — файл видео (поддерживает Range: докачка после обрыва)
    POST /plan/<id>/result     — ответ ПК: что выложено, а что нет (JSON)
    GET  /profiles             — профили ManyFaces, о которых ПК сообщил последним
    POST /profiles             — ПК сообщает свои профили: {"profiles": [{"id": "...", "name": "..."}]};
                                 по этому списку бот проверяет id профиля, когда добавляют аккаунт

Всё, кроме /health, требует заголовок ``Authorization: Bearer <MOTHER_API_TOKEN>``.
Сам по себе сервер не шифрует трафик: наружу его выставляют только за HTTPS-прокси
(Caddy/nginx) или внутри закрытой сети (Tailscale/WireGuard)."""

import hmac
import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db
import plan_files

logger = logging.getLogger(__name__)

CHUNK = 1024 * 1024
MAX_BODY = 1024 * 1024
PLAN_RE = re.compile(r"^/plan/(\d+)$")
RESULT_RE = re.compile(r"^/plan/(\d+)/result$")
VIDEO_RE = re.compile(r"^/video/([^/]+)$")
PROFILE_ID_RE = re.compile(r"^[0-9a-zA-Z_-]{1,64}$")
MAX_PROFILES = 1000


def make_handler(token: str, video_dir: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "mother"

        def log_message(self, fmt, *args):      # в общий лог бота, без шума в stderr
            logger.info("api %s - %s", self.address_string(), fmt % args)

        # ---- ответы ----
        def _json(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            sent = self.headers.get("Authorization", "")
            return bool(token) and hmac.compare_digest(sent.encode(), f"Bearer {token}".encode())

        # ---- GET ----
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/health":
                return self._json(200, {"ok": True})
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            if path == "/plan/latest":
                row = db.get_pending_plan()
                return self._plan(row, "нет плана, ожидающего выкладки")
            if path == "/plan/retry":
                return self._plan(db.get_pending_retry(), "нет повтора, ожидающего выкладки")
            m = PLAN_RE.match(path)
            if m:
                return self._plan(db.get_plan(int(m.group(1))), "нет такого плана")
            m = VIDEO_RE.match(path)
            if m:
                return self._video(m.group(1))
            if path == "/profiles":
                return self._json(200, {"synced_at": db.pc_profiles_synced_at(), "profiles": [
                    {"id": r["profile_id"], "name": r["name"]} for r in db.get_pc_profiles()]})
            self._json(404, {"error": "not found"})

        def _plan(self, row, missing: str) -> None:
            if row is None:
                return self._json(404, {"error": missing})
            self._json(200, {**json.loads(row["body"]), "acked": row["result"] is not None})

        def _video(self, name: str) -> None:
            path = plan_files.safe_video_path(video_dir, name)
            if not path:
                return self._json(404, {"error": "no such video"})
            size = os.path.getsize(path)
            start = 0
            rng = self.headers.get("Range")
            if rng:
                m = re.match(r"^bytes=(\d+)-$", rng.strip())
                if not m or int(m.group(1)) >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                start = int(m.group(1))
            self.send_response(206 if start else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size - start))
            if start:
                self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                while chunk := f.read(CHUNK):
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return          # ПК оборвал загрузку — доберёт с Range

        # ---- POST ----
        def do_POST(self):
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            path = self.path.split("?", 1)[0]
            m = RESULT_RE.match(path)
            if not m and path != "/profiles":
                return self._json(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                return self._json(400, {"error": "нужно тело JSON до 1 МБ"})
            try:
                result = json.loads(self.rfile.read(length))
            except ValueError:
                return self._json(400, {"error": "тело не JSON"})
            if not isinstance(result, dict):
                return self._json(400, {"error": "ожидается JSON-объект"})
            if not m:
                return self._profiles(result)
            if not db.set_plan_result(int(m.group(1)), result):
                return self._json(404, {"error": "нет такого плана"})
            logger.info("api: получен результат плана %s", m.group(1))
            self._json(200, {"ok": True})

        def _profiles(self, body: dict) -> None:
            profiles = body.get("profiles")
            if not isinstance(profiles, list) or len(profiles) > MAX_PROFILES:
                return self._json(400, {"error": f"нужен список profiles (до {MAX_PROFILES})"})
            for p in profiles:
                if not isinstance(p, dict) or not PROFILE_ID_RE.match(str(p.get("id") or "")):
                    return self._json(400, {"error": "у каждого профиля нужен id (буквы, цифры, _ и -)"})
            n = db.set_pc_profiles(profiles)
            logger.info("api: ПК сообщил профили ManyFaces: %s", n)
            self._json(200, {"ok": True, "profiles": n})

    return Handler


def serve(host: str, port: int, token: str, video_dir: str) -> ThreadingHTTPServer:
    """Создать сервер (port=0 — любой свободный). Запуск: ``server.serve_forever()``."""
    if not token:
        raise ValueError("MOTHER_API_TOKEN не задан — API без токена не запускаем")
    return ThreadingHTTPServer((host, port), make_handler(token, video_dir))


def start_in_thread(host: str, port: int, token: str, video_dir: str) -> ThreadingHTTPServer:
    server = serve(host, port, token, video_dir)
    threading.Thread(target=server.serve_forever, name="mother-api", daemon=True).start()
    logger.info("API для ПК слушает %s:%s", host, port)
    return server
