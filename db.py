"""SQLite-хранилище mother.

Модель данных (связь источник↔группа — многие-ко-многим через «правила»):

    users         — телеграм-пользователь
    accounts      — аккаунты: страница VK (id пользователя) + профиль ManyFaces на ПК, вошедший в неё
    pc_profiles   — профили ManyFaces, о которых сообщил ПК (из них выбирают профиль аккаунта)
    groups        — целевые группы (числовой id группы вводится вручную, к VK не обращаемся);
                    группа может быть привязана к аккаунту (account_id)
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

            CREATE TABLE IF NOT EXISTS accounts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id  INTEGER NOT NULL,
                vk_user_id   INTEGER NOT NULL,                  -- страница VK: vk.com/id<vk_user_id>
                name         TEXT NOT NULL,
                profile_id   TEXT,                              -- профиль ManyFaces на ПК; NULL — по умолчанию
                profile_name TEXT,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                UNIQUE (telegram_id, vk_user_id)
            );

            CREATE TABLE IF NOT EXISTS pc_profiles (
                profile_id  TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                seen_at     INTEGER NOT NULL
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
                cleaned     INTEGER NOT NULL DEFAULT 0,         -- файлы плана уже удалены
                notified    INTEGER NOT NULL DEFAULT 0,         -- об итоге выкладки уже написали в Telegram
                retry_of    INTEGER                             -- повтор: id плана, видео которого не вышли
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
        if "notified" not in plan_cols:
            conn.execute("ALTER TABLE plans ADD COLUMN notified INTEGER NOT NULL DEFAULT 0")
            # итоги, пришедшие до появления уведомлений, повторно не присылаем
            conn.execute("UPDATE plans SET notified = 1 WHERE result IS NOT NULL")
        if "retry_of" not in plan_cols:
            conn.execute("ALTER TABLE plans ADD COLUMN retry_of INTEGER")
        group_cols = {row[1] for row in conn.execute("PRAGMA table_info(groups)")}
        if "account_id" not in group_cols:
            # удалили аккаунт — группа остаётся, просто без аккаунта
            conn.execute("ALTER TABLE groups ADD COLUMN account_id INTEGER "
                         "REFERENCES accounts(id) ON DELETE SET NULL")
        source_cols = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
        if "owner_account_id" not in source_cols:
            # чей источник: NULL — общий (виден во всех аккаунтах), иначе — аккаунта, в котором его добавили
            conn.execute("ALTER TABLE sources ADD COLUMN owner_account_id INTEGER "
                         "REFERENCES accounts(id) ON DELETE SET NULL")
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


# ─── Аккаунты (страница VK + профиль ManyFaces) ───────────────────────────────

def get_accounts(telegram_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT a.*, (SELECT COUNT(*) FROM groups g WHERE g.account_id = a.id) AS n_groups "
            "FROM accounts a WHERE a.telegram_id = ? ORDER BY a.id", (telegram_id,)
        ).fetchall()


def get_account(account_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()


def add_account(telegram_id: int, vk_user_id: int, name: str,
                profile_id: str | None = None, profile_name: str | None = None) -> int:
    """Добавить аккаунт (та же страница VK второй раз — обновит название и профиль). Возвращает id строки."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO accounts (telegram_id, vk_user_id, name, profile_id, profile_name) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id, vk_user_id) DO UPDATE SET
                name = excluded.name, profile_id = excluded.profile_id, profile_name = excluded.profile_name
            """,
            (telegram_id, vk_user_id, name, profile_id, profile_name),
        )
        return conn.execute("SELECT id FROM accounts WHERE telegram_id = ? AND vk_user_id = ?",
                            (telegram_id, vk_user_id)).fetchone()["id"]


def set_account_profile(account_id: int, profile_id: str | None, profile_name: str | None) -> None:
    with _connect() as conn:
        conn.execute("UPDATE accounts SET profile_id = ?, profile_name = ? WHERE id = ?",
                     (profile_id, profile_name, account_id))


def rename_account(account_id: int, name: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE accounts SET name = ? WHERE id = ?", (name, account_id))


def delete_account(account_id: int) -> None:
    """Группы аккаунта остаются, но без аккаунта."""
    with _connect() as conn:
        conn.execute("UPDATE groups SET account_id = NULL WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))


def set_group_account(group_row_id: int, account_id: int | None) -> None:
    with _connect() as conn:
        conn.execute("UPDATE groups SET account_id = ? WHERE id = ?", (account_id, group_row_id))


# ─── Профили, о которых сообщил ПК ────────────────────────────────────────────

def set_pc_profiles(profiles: list[dict]) -> int:
    """Заменить список профилей ManyFaces с ПК: [{"id": "...", "name": "..."}]. Возвращает их число."""
    now = int(time.time())
    rows = [(str(p["id"]).strip().lower(), str(p.get("name") or "")[:100], now) for p in profiles]
    with _connect() as conn:
        conn.execute("DELETE FROM pc_profiles")
        conn.executemany("INSERT OR REPLACE INTO pc_profiles (profile_id, name, seen_at) VALUES (?, ?, ?)", rows)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('pc_profiles_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(now),))
    return len(rows)


def get_pc_profiles() -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM pc_profiles ORDER BY name").fetchall()


def get_pc_profile(profile_id: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM pc_profiles WHERE profile_id = ?",
                            (profile_id.strip().lower(),)).fetchone()


def pc_profiles_synced_at() -> int | None:
    value = get_setting("pc_profiles_at")
    return int(value) if value else None


# ─── Группы ───────────────────────────────────────────────────────────────────

def _scope(column: str, scope) -> tuple[str, tuple]:
    """Отбор по аккаунту для меню бота. scope: None — всё, 0 — без аккаунта, N — аккаунт N."""
    if scope is None:
        return "", ()
    if scope == 0:
        return f" AND {column} IS NULL", ()
    return f" AND {column} = ?", (scope,)


def get_groups(telegram_id: int, scope=None) -> list[sqlite3.Row]:
    where, args = _scope("g.account_id", scope)
    with _connect() as conn:
        return conn.execute(
            "SELECT g.*, a.name AS account_name, a.vk_user_id AS account_vk_id, a.profile_id AS profile_id "
            "FROM groups g LEFT JOIN accounts a ON a.id = g.account_id "
            "WHERE g.telegram_id = ?" + where + " ORDER BY g.id", (telegram_id, *args)
        ).fetchall()


def get_group(group_row_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM groups WHERE id = ?", (group_row_id,)).fetchone()


def add_group(telegram_id: int, vk_group_id: int, name: str) -> int:
    """Добавить группу (та же группа второй раз — только обновит название). Возвращает id строки."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO groups (telegram_id, vk_group_id, name) VALUES (?, ?, ?)
            ON CONFLICT(telegram_id, vk_group_id) DO UPDATE SET name = excluded.name
            """,
            (telegram_id, vk_group_id, name),
        )
        return conn.execute("SELECT id FROM groups WHERE telegram_id = ? AND vk_group_id = ?",
                            (telegram_id, vk_group_id)).fetchone()["id"]


def rename_group(group_row_id: int, name: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE groups SET name = ? WHERE id = ?", (name, group_row_id))


def delete_group(group_row_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM groups WHERE id = ?", (group_row_id,))


# ─── Источники ────────────────────────────────────────────────────────────────

def get_sources(telegram_id: int, scope=None) -> list[sqlite3.Row]:
    """scope: None — все; иначе источники этого аккаунта и общие (без владельца)."""
    where, args = "", ()
    if scope:
        where, args = " AND (owner_account_id = ? OR owner_account_id IS NULL)", (scope,)
    elif scope == 0:
        where = " AND owner_account_id IS NULL"
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM sources WHERE telegram_id = ?" + where + " ORDER BY id", (telegram_id, *args)
        ).fetchall()


def get_source(source_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()


def add_source(
    telegram_id: int, name: str, db_path: str, username: str | None,
    *, kind: str = "db", account: str | None = None, owner_account_id: int | None = None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO sources (telegram_id, name, db_path, username, kind, account, owner_account_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (telegram_id, name, db_path, username, kind, account, owner_account_id or None),
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


def get_rules(telegram_id: int, scope=None) -> list[sqlite3.Row]:
    """scope: None — все правила; 0 — правила групп без аккаунта; N — правила групп аккаунта N."""
    where, args = _scope("g.account_id", scope)
    with _connect() as conn:
        return conn.execute(
            _RULE_SELECT + " WHERE r.telegram_id = ?" + where + " ORDER BY r.id", (telegram_id, *args)
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


def reset_queue_day(telegram_id: int, plan_day: str) -> int:
    """Обнулить норму дня: строки очереди этого дня перестают в неё засчитываться.

    Не удаляются и не повторяются: видео остаются в журнале выдачи (``delivered``), поэтому в новую
    порцию они не попадут, а лишь освобождают место в дневной норме. Возвращает число строк."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE queue SET plan_day = plan_day || '+reset' WHERE telegram_id = ? AND plan_day = ?",
            (telegram_id, plan_day),
        )
        return cur.rowcount


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
        "r.description AS rule_description, a.vk_user_id AS account_vk_id, a.name AS account_name, "
        "a.profile_id AS profile_id, a.profile_name AS profile_name "
        "FROM queue q JOIN rules r ON r.id = q.rule_id JOIN groups g ON g.id = r.group_id "
        "LEFT JOIN accounts a ON a.id = g.account_id "
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
            "INSERT INTO plans (id, telegram_id, plan_day, created_at, body, retry_of) VALUES (?, ?, ?, ?, ?, ?)",
            (body["id"], telegram_id, plan_day, int(time.time()),
             json.dumps(body, ensure_ascii=False), body.get("retry_of")),
        )


def get_plan(plan_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()


def get_pending_plan() -> sqlite3.Row | None:
    """Самый свежий ежедневный план, о котором ПК ещё не сообщил результат (повторы — отдельно)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM plans WHERE result IS NULL AND cleaned = 0 AND retry_of IS NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()


def get_pending_retry() -> sqlite3.Row | None:
    """Самый старый план-повтор, который ПК ещё не выложил."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM plans WHERE result IS NULL AND cleaned = 0 AND retry_of IS NOT NULL "
            "ORDER BY id LIMIT 1"
        ).fetchone()


def get_retry_for(plan_id: int) -> sqlite3.Row | None:
    """Повтор, уже созданный для плана plan_id (если есть)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM plans WHERE retry_of = ? ORDER BY id DESC LIMIT 1", (plan_id,)
        ).fetchone()


def get_unfinished_plans() -> list[sqlite3.Row]:
    """Планы, которые ПК ещё не выложил: их файлы удалять нельзя."""
    with _connect() as conn:
        return conn.execute("SELECT * FROM plans WHERE result IS NULL AND cleaned = 0").fetchall()


def set_plan_result(plan_id: int, result: dict) -> bool:
    """Записать ответ ПК. False — такого плана нет."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE plans SET result = ?, acked_at = ? WHERE id = ?",
            (json.dumps(result, ensure_ascii=False), int(time.time()), plan_id),
        )
        return cur.rowcount > 0


def get_unnotified_results() -> list[sqlite3.Row]:
    """Планы, итог которых пришёл с ПК, а в Telegram о нём ещё не написали."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM plans WHERE result IS NOT NULL AND notified = 0 ORDER BY id"
        ).fetchall()


def mark_plan_notified(plan_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE plans SET notified = 1 WHERE id = ?", (plan_id,))


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


# ─── Текущий аккаунт в боте (как в omega: меню показывает только его) ──────────

def get_current_account(telegram_id: int) -> int | None:
    """Выбранный в боте аккаунт: id, 0 — «без аккаунта», None — не выбран."""
    value = get_setting(f"current_account:{telegram_id}")
    return int(value) if value not in (None, "") else None


def set_current_account(telegram_id: int, account_id: int | None) -> None:
    set_setting(f"current_account:{telegram_id}", "" if account_id is None else int(account_id))
