"""SQLite-хранилище mother.

Модель данных (связь источник↔группа — многие-ко-многим через «правила»):

    users         — телеграм-пользователь
    groups        — целевые группы (числовой id группы вводится вручную, к VK не обращаемся)
    sources       — источники видео (база парсера или аккаунт TikTok)
    rules         — правило = пара (источник → группа) + настройки выдачи
    delivered     — что уже выдано по каждому правилу (дедуп)
    queue         — видео на выдачу: pending → ready (скачано) → delivered
    plans         — выданные JSON-планы
    settings      — глобальные настройки (ключ→значение)
    error_logs    — журнал ошибок для /errors
"""

import json
import os
import sqlite3
import time

DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "mother.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS groups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                vk_group_id INTEGER NOT NULL,
                name        TEXT NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                UNIQUE (telegram_id, vk_group_id)
            );

            CREATE TABLE IF NOT EXISTS sources (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id   INTEGER NOT NULL,
                name          TEXT NOT NULL,
                db_path       TEXT NOT NULL,
                username      TEXT,
                kind          TEXT NOT NULL DEFAULT 'db',
                account       TEXT,
                backfill_done INTEGER NOT NULL DEFAULT 0,
                parse_start   INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS rules (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id    INTEGER NOT NULL,
                source_id      INTEGER NOT NULL,
                group_id       INTEGER NOT NULL,
                videos_per_day INTEGER NOT NULL DEFAULT 3,
                description    TEXT,
                min_duration   INTEGER,
                max_duration   INTEGER,
                order_dir      TEXT NOT NULL DEFAULT 'old',
                enabled        INTEGER NOT NULL DEFAULT 1,
                allow_repost   INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                FOREIGN KEY (source_id)   REFERENCES sources(id)        ON DELETE CASCADE,
                FOREIGN KEY (group_id)    REFERENCES groups(id)         ON DELETE CASCADE,
                UNIQUE (source_id, group_id)
            );

            CREATE TABLE IF NOT EXISTS delivered (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id      INTEGER NOT NULL,
                tt_video_id  TEXT NOT NULL,
                delivered_at INTEGER NOT NULL,
                FOREIGN KEY (rule_id) REFERENCES rules(id) ON DELETE CASCADE,
                UNIQUE (rule_id, tt_video_id)
            );

            CREATE TABLE IF NOT EXISTS queue (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                rule_id     INTEGER NOT NULL,
                tt_video_id TEXT NOT NULL,
                url         TEXT NOT NULL,
                title       TEXT,
                video_description TEXT,                         -- подпись к видео из источника
                status      TEXT NOT NULL DEFAULT 'pending',   -- pending | ready | delivered
                file_path   TEXT,
                plan_day    TEXT NOT NULL,                      -- YYYY-MM-DD (МСК)
                created_at  INTEGER NOT NULL,
                FOREIGN KEY (rule_id) REFERENCES rules(id) ON DELETE CASCADE,
                UNIQUE (rule_id, tt_video_id)
            );

            CREATE INDEX IF NOT EXISTS idx_queue_rule ON queue (rule_id);

            CREATE TABLE IF NOT EXISTS plans (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                plan_day    TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                body        TEXT NOT NULL,
                result      TEXT,                               -- ответ ПК: что выложено
                acked_at    INTEGER,
                cleaned     INTEGER NOT NULL DEFAULT 0          -- файлы плана уже удалены
            );

            CREATE TABLE IF NOT EXISTS description_templates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                title       TEXT NOT NULL,
                body        TEXT NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS error_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                created_at  INTEGER NOT NULL,
                stage       TEXT,
                platform    TEXT,
                url         TEXT,
                group_name  TEXT,
                message     TEXT,
                traceback   TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_error_logs_user
                ON error_logs (telegram_id, created_at DESC);
            """
        )
        # Миграция: БД, созданные до появления описаний
        rule_cols = {row[1] for row in conn.execute("PRAGMA table_info(rules)")}
        if "description" not in rule_cols:
            conn.execute("ALTER TABLE rules ADD COLUMN description TEXT")
        plan_cols = {row[1] for row in conn.execute("PRAGMA table_info(plans)")}
        for col, defn in (("result", "TEXT"), ("acked_at", "INTEGER"),
                          ("cleaned", "INTEGER NOT NULL DEFAULT 0")):
            if col not in plan_cols:
                conn.execute(f"ALTER TABLE plans ADD COLUMN {col} {defn}")
        queue_cols = {row[1] for row in conn.execute("PRAGMA table_info(queue)")}
        if "video_description" not in queue_cols:
            conn.execute("ALTER TABLE queue ADD COLUMN video_description TEXT")


# ─── Пользователи ─────────────────────────────────────────────────────────────

def ensure_user(telegram_id: int) -> None:
    with _connect() as conn:
        conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (telegram_id,))


def get_user_ids() -> list[int]:
    with _connect() as conn:
        return [r["telegram_id"] for r in conn.execute("SELECT telegram_id FROM users")]


# ─── Группы ───────────────────────────────────────────────────────────────────

def get_groups(telegram_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM groups WHERE telegram_id = ? ORDER BY id", (telegram_id,)
        ).fetchall()


def get_group(group_row_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM groups WHERE id = ?", (group_row_id,)).fetchone()


def add_group(telegram_id: int, vk_group_id: int, name: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO groups (telegram_id, vk_group_id, name) VALUES (?, ?, ?)
            ON CONFLICT(telegram_id, vk_group_id) DO UPDATE SET name = excluded.name
            """,
            (telegram_id, vk_group_id, name),
        )


def rename_group(group_row_id: int, name: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE groups SET name = ? WHERE id = ?", (name, group_row_id))


def delete_group(group_row_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM groups WHERE id = ?", (group_row_id,))


# ─── Источники ────────────────────────────────────────────────────────────────

def get_sources(telegram_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM sources WHERE telegram_id = ? ORDER BY id", (telegram_id,)
        ).fetchall()


def get_source(source_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()


def add_source(
    telegram_id: int, name: str, db_path: str, username: str | None,
    *, kind: str = "db", account: str | None = None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO sources (telegram_id, name, db_path, username, kind, account) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (telegram_id, name, db_path, username, kind, account),
        )
        return cur.lastrowid


def set_source_db_path(source_id: int, db_path: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE sources SET db_path = ? WHERE id = ?", (db_path, source_id))


def set_source_parse_start(source_id: int, parse_start: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE sources SET parse_start = ? WHERE id = ?",
            (max(int(parse_start), 1), source_id),
        )


def get_account_sources() -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM sources WHERE kind = 'account'").fetchall()


def set_backfill_done(source_id: int, done: bool = True) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE sources SET backfill_done = ? WHERE id = ?", (1 if done else 0, source_id)
        )


def delete_source(source_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


# ─── Правила ──────────────────────────────────────────────────────────────────

_RULE_SELECT = """
    SELECT r.*, s.name AS source_name, s.db_path AS db_path, s.username AS username,
           s.kind AS source_kind,
           g.name AS group_name, g.vk_group_id AS vk_group_id
    FROM rules r
    JOIN sources s ON s.id = r.source_id
    JOIN groups  g ON g.id = r.group_id
"""


def get_rules(telegram_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            _RULE_SELECT + " WHERE r.telegram_id = ? ORDER BY r.id", (telegram_id,)
        ).fetchall()


def get_enabled_rules() -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            _RULE_SELECT + " WHERE r.enabled = 1 ORDER BY r.telegram_id, r.id"
        ).fetchall()


def get_rule(rule_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(_RULE_SELECT + " WHERE r.id = ?", (rule_id,)).fetchone()


def add_rule(
    telegram_id: int, source_id: int, group_id: int,
    *, videos_per_day: int = 3, description: str | None = None,
    min_duration: int | None = None, max_duration: int | None = None,
    order_dir: str = "old",
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO rules
                (telegram_id, source_id, group_id, videos_per_day, description,
                 min_duration, max_duration, order_dir, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (telegram_id, source_id, group_id, videos_per_day, description,
             min_duration, max_duration, order_dir),
        )
        return cur.lastrowid


def update_rule(rule_id: int, **fields) -> None:
    allowed = {"videos_per_day", "description", "min_duration", "max_duration",
               "order_dir", "enabled", "allow_repost"}
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"Недопустимое поле правила: {key}")
        sets.append(f"{key} = ?")
        values.append(value)
    if not sets:
        return
    values.append(rule_id)
    with _connect() as conn:
        conn.execute(f"UPDATE rules SET {', '.join(sets)} WHERE id = ?", values)


def delete_rule(rule_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))


# ─── Дедуп выдачи ─────────────────────────────────────────────────────────────

def get_delivered_ids(rule_id: int) -> set[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tt_video_id FROM delivered WHERE rule_id = ?", (rule_id,)
        ).fetchall()
    return {r["tt_video_id"] for r in rows}


def clear_delivered(rule_id: int) -> None:
    """Сбросить историю выдачи правила (режим перезаливки)."""
    with _connect() as conn:
        conn.execute("DELETE FROM delivered WHERE rule_id = ?", (rule_id,))
        conn.execute("DELETE FROM queue WHERE rule_id = ? AND status = 'delivered'", (rule_id,))


def mark_delivered(rule_id: int, tt_video_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO delivered (rule_id, tt_video_id, delivered_at) VALUES (?, ?, ?)",
            (rule_id, tt_video_id, int(time.time())),
        )


def count_delivered(rule_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM delivered WHERE rule_id = ?", (rule_id,)
        ).fetchone()
    return row["c"] if row else 0


# ─── Очередь выдачи ───────────────────────────────────────────────────────────

def get_queued_ids(rule_id: int) -> set[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tt_video_id FROM queue WHERE rule_id = ?", (rule_id,)
        ).fetchall()
    return {r["tt_video_id"] for r in rows}


def count_queue_for_day(rule_id: int, plan_day: str) -> int:
    """Сколько видео правила уже стоит в очереди на день выдачи (включая выданные)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM queue WHERE rule_id = ? AND plan_day = ?",
            (rule_id, plan_day),
        ).fetchone()
    return row["c"] if row else 0


def add_queue_item(
    *, telegram_id: int, rule_id: int, tt_video_id: str, url: str,
    title: str | None, plan_day: str, video_description: str | None = None,
) -> int | None:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO queue
                (telegram_id, rule_id, tt_video_id, url, title, video_description,
                 plan_day, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, rule_id, tt_video_id, url, title, video_description, plan_day,
             int(time.time())),
        )
        return cur.lastrowid if cur.rowcount else None


def get_queue(status: str | None = None, telegram_id: int | None = None) -> list[sqlite3.Row]:
    sql = (
        "SELECT q.*, g.vk_group_id AS vk_group_id, g.name AS group_name, "
        "r.description AS rule_description "
        "FROM queue q JOIN rules r ON r.id = q.rule_id JOIN groups g ON g.id = r.group_id "
        "WHERE 1=1"
    )
    params: list = []
    if status:
        sql += " AND q.status = ?"
        params.append(status)
    if telegram_id is not None:
        sql += " AND q.telegram_id = ?"
        params.append(telegram_id)
    sql += " ORDER BY q.id"
    with _connect() as conn:
        return conn.execute(sql, params).fetchall()


def get_queue_item(item_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()


def mark_queue_ready(item_id: int, file_path: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE queue SET status = 'ready', file_path = ? WHERE id = ?", (file_path, item_id)
        )


def mark_queue_delivered(item_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE queue SET status = 'delivered' WHERE id = ?", (item_id,))


def delete_queue_item(item_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM queue WHERE id = ?", (item_id,))


# ─── Планы ────────────────────────────────────────────────────────────────────

def next_plan_id() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM plans").fetchone()
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'plans'"
        ).fetchone()
    return max(row["n"], (seq["seq"] + 1) if seq else 1)


def save_plan(telegram_id: int, plan_day: str, body: dict) -> None:
    """Сохранить выданный план; body['id'] становится id строки."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO plans (id, telegram_id, plan_day, created_at, body) VALUES (?, ?, ?, ?, ?)",
            (body["id"], telegram_id, plan_day, int(time.time()),
             json.dumps(body, ensure_ascii=False)),
        )


def get_plan(plan_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()


def get_pending_plan() -> sqlite3.Row | None:
    """Самый свежий план, о котором ПК ещё не сообщил результат."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM plans WHERE result IS NULL AND cleaned = 0 ORDER BY id DESC LIMIT 1"
        ).fetchone()


def set_plan_result(plan_id: int, result: dict) -> bool:
    """Записать ответ ПК. False — такого плана нет."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE plans SET result = ?, acked_at = ? WHERE id = ?",
            (json.dumps(result, ensure_ascii=False), int(time.time()), plan_id),
        )
        return cur.rowcount > 0


def get_plans_to_clean(acked_before_ts: int) -> list[sqlite3.Row]:
    """Планы, по которым результат давно получен, а файлы ещё на диске."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM plans WHERE result IS NOT NULL AND cleaned = 0 AND acked_at < ?",
            (acked_before_ts,),
        ).fetchall()


def mark_plan_cleaned(plan_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE plans SET cleaned = 1 WHERE id = ?", (plan_id,))


def get_plan_for_day(telegram_id: int, plan_day: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM plans WHERE telegram_id = ? AND plan_day = ? ORDER BY id DESC LIMIT 1",
            (telegram_id, plan_day),
        ).fetchone()


# ─── Заготовки описаний ───────────────────────────────────────────────────────

def get_templates(telegram_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM description_templates WHERE telegram_id = ? ORDER BY id",
            (telegram_id,),
        ).fetchall()


def get_template(template_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM description_templates WHERE id = ?", (template_id,)
        ).fetchone()


def add_template(telegram_id: int, title: str, body: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO description_templates (telegram_id, title, body) VALUES (?, ?, ?)",
            (telegram_id, title, body),
        )


def update_template(template_id: int, title: str, body: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE description_templates SET title = ?, body = ? WHERE id = ?",
            (title, body, template_id),
        )


def delete_template(template_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM description_templates WHERE id = ?", (template_id,))


# ─── Глобальные настройки ─────────────────────────────────────────────────────

def get_setting(key: str, default: str | None = None) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


# ─── Логи ошибок ──────────────────────────────────────────────────────────────

def log_error(
    telegram_id: int, *, stage: str | None = None, platform: str | None = None,
    url: str | None = None, group_name: str | None = None,
    message: str | None = None, traceback: str | None = None,
) -> None:
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO error_logs
                    (telegram_id, created_at, stage, platform, url, group_name, message, traceback)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (telegram_id, int(time.time()), stage, platform, url, group_name,
                 message, traceback),
            )
    except Exception:
        pass


def get_errors(telegram_id: int, limit: int = 8, offset: int = 0) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM error_logs WHERE telegram_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (telegram_id, limit, offset),
        ).fetchall()


def count_errors(telegram_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM error_logs WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return row["c"] if row else 0


def cleanup_old_errors(days: int) -> int:
    cutoff = int(time.time()) - days * 86400
    with _connect() as conn:
        cur = conn.execute("DELETE FROM error_logs WHERE created_at < ?", (cutoff,))
        return cur.rowcount
