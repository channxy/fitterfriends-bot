import os
from datetime import date
from typing import Optional

try:
    import libsql_experimental as libsql
    _BACKEND = "libsql"
except ImportError:
    import sqlite3 as libsql  # type: ignore[no-redef]
    _BACKEND = "sqlite3"


class _Row(dict):
    """Dict that also supports integer index access, mimicking sqlite3.Row."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class Database:
    def __init__(self, url: str):
        self.url = url
        self._token = os.environ.get("TURSO_AUTH_TOKEN", "")
        self._init()

    def _conn(self):
        if _BACKEND == "libsql":
            if self._token:
                return libsql.connect(self.url, auth_token=self._token)
            # local file for libsql (strip libsql:// prefix if present)
            local = self.url if not self.url.startswith("libsql://") else "fitterfriends.db"
            return libsql.connect(local)
        else:
            # sqlite3 fallback (local dev when libsql-experimental not installed)
            local = self.url if not self.url.startswith("libsql://") else "fitterfriends.db"
            conn = libsql.connect(local)
            conn.row_factory = None  # we handle rows ourselves via _row/_rows
            return conn

    def _row(self, cursor) -> Optional[_Row]:
        row = cursor.fetchone()
        if row is None or cursor.description is None:
            return None
        cols = [d[0] for d in cursor.description]
        return _Row(zip(cols, row))

    def _rows(self, cursor) -> list:
        if cursor.description is None:
            return []
        cols = [d[0] for d in cursor.description]
        return [_Row(zip(cols, r)) for r in cursor.fetchall()]

    def _init(self):
        conn = self._conn()
        try:
            if _BACKEND == "sqlite3":
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
            statements = [
                """CREATE TABLE IF NOT EXISTS groups (
                    chat_id      INTEGER PRIMARY KEY,
                    leader_id    INTEGER NOT NULL,
                    reset_day    INTEGER NOT NULL DEFAULT 6,
                    payment_mode TEXT NOT NULL DEFAULT 'log',
                    payment_target TEXT
                )""",
                """CREATE TABLE IF NOT EXISTS group_goals (
                    chat_id        INTEGER NOT NULL,
                    goal_type      TEXT NOT NULL,
                    daily_penalty  REAL NOT NULL DEFAULT 0,
                    weekly_penalty REAL NOT NULL DEFAULT 0,
                    run_unit       TEXT DEFAULT 'km',
                    PRIMARY KEY (chat_id, goal_type)
                )""",
                """CREATE TABLE IF NOT EXISTS members (
                    user_id  INTEGER NOT NULL,
                    chat_id  INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    timezone TEXT DEFAULT 'UTC',
                    PRIMARY KEY (user_id, chat_id)
                )""",
                """CREATE TABLE IF NOT EXISTS member_targets (
                    user_id   INTEGER NOT NULL,
                    chat_id   INTEGER NOT NULL,
                    goal_type TEXT NOT NULL,
                    target    REAL,
                    target2   REAL,
                    period    TEXT DEFAULT 'daily',
                    PRIMARY KEY (user_id, chat_id, goal_type)
                )""",
                """CREATE TABLE IF NOT EXISTS calorie_logs (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id   INTEGER NOT NULL,
                    chat_id   INTEGER NOT NULL,
                    log_date  TEXT NOT NULL,
                    calories  INTEGER NOT NULL,
                    label     TEXT,
                    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
                """CREATE TABLE IF NOT EXISTS activity_logs (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id   INTEGER NOT NULL,
                    chat_id   INTEGER NOT NULL,
                    goal_type TEXT NOT NULL,
                    log_date  TEXT NOT NULL,
                    value     REAL NOT NULL,
                    label     TEXT,
                    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
                """CREATE TABLE IF NOT EXISTS weight_logs (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id   INTEGER NOT NULL,
                    chat_id   INTEGER NOT NULL,
                    log_date  TEXT NOT NULL,
                    weight_kg REAL NOT NULL,
                    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
                """CREATE TABLE IF NOT EXISTS debts (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL,
                    chat_id    INTEGER NOT NULL,
                    amount     REAL NOT NULL,
                    reason     TEXT,
                    date       TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
                """CREATE TABLE IF NOT EXISTS payments (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL,
                    chat_id    INTEGER NOT NULL,
                    amount     REAL NOT NULL,
                    date       TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
                """CREATE TABLE IF NOT EXISTS penalty_log (
                    user_id    INTEGER NOT NULL,
                    chat_id    INTEGER NOT NULL,
                    goal_type  TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    PRIMARY KEY (user_id, chat_id, goal_type, period_key)
                )""",
                # weight_logs unique constraint via index (can't use UNIQUE in CREATE after the fact easily)
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_weight_unique
                   ON weight_logs (user_id, chat_id, log_date)""",
            ]
            for stmt in statements:
                conn.execute(stmt)
            conn.commit()
        finally:
            conn.close()

    # ── Groups ────────────────────────────────────────────────────────────────────

    def save_group(self, chat_id, leader_id, reset_day, payment_mode, payment_target):
        conn = self._conn()
        try:
            conn.execute("""
                INSERT INTO groups (chat_id, leader_id, reset_day, payment_mode, payment_target)
                VALUES (?,?,?,?,?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    leader_id=excluded.leader_id, reset_day=excluded.reset_day,
                    payment_mode=excluded.payment_mode, payment_target=excluded.payment_target
            """, (chat_id, leader_id, reset_day, payment_mode, payment_target))
            conn.commit()
        finally:
            conn.close()

    def get_group(self, chat_id) -> Optional[_Row]:
        conn = self._conn()
        try:
            return self._row(conn.execute("SELECT * FROM groups WHERE chat_id=?", (chat_id,)))
        finally:
            conn.close()

    def get_all_groups(self):
        conn = self._conn()
        try:
            return self._rows(conn.execute("SELECT * FROM groups"))
        finally:
            conn.close()

    # ── Group goals ───────────────────────────────────────────────────────────────

    def save_group_goal(self, chat_id, goal_type, daily_penalty, weekly_penalty, run_unit="km"):
        conn = self._conn()
        try:
            conn.execute("""
                INSERT INTO group_goals (chat_id, goal_type, daily_penalty, weekly_penalty, run_unit)
                VALUES (?,?,?,?,?)
                ON CONFLICT(chat_id, goal_type) DO UPDATE SET
                    daily_penalty=excluded.daily_penalty,
                    weekly_penalty=excluded.weekly_penalty,
                    run_unit=excluded.run_unit
            """, (chat_id, goal_type, daily_penalty, weekly_penalty, run_unit))
            conn.commit()
        finally:
            conn.close()

    def get_group_goals(self, chat_id):
        conn = self._conn()
        try:
            return self._rows(conn.execute(
                "SELECT * FROM group_goals WHERE chat_id=?", (chat_id,)
            ))
        finally:
            conn.close()

    def get_group_goal(self, chat_id, goal_type) -> Optional[_Row]:
        conn = self._conn()
        try:
            return self._row(conn.execute(
                "SELECT * FROM group_goals WHERE chat_id=? AND goal_type=?",
                (chat_id, goal_type)
            ))
        finally:
            conn.close()

    def delete_group_goals(self, chat_id):
        conn = self._conn()
        try:
            conn.execute("DELETE FROM group_goals WHERE chat_id=?", (chat_id,))
            conn.commit()
        finally:
            conn.close()

    # ── Members ───────────────────────────────────────────────────────────────────

    def ensure_member(self, user_id, chat_id, username):
        conn = self._conn()
        try:
            conn.execute("""
                INSERT INTO members (user_id, chat_id, username)
                VALUES (?,?,?)
                ON CONFLICT(user_id, chat_id) DO UPDATE SET username=excluded.username
            """, (user_id, chat_id, username))
            conn.commit()
        finally:
            conn.close()

    def get_member(self, user_id, chat_id) -> Optional[_Row]:
        conn = self._conn()
        try:
            return self._row(conn.execute(
                "SELECT * FROM members WHERE user_id=? AND chat_id=?", (user_id, chat_id)
            ))
        finally:
            conn.close()

    def get_all_members(self, chat_id):
        conn = self._conn()
        try:
            return self._rows(conn.execute("SELECT * FROM members WHERE chat_id=?", (chat_id,)))
        finally:
            conn.close()

    def set_member_timezone(self, user_id, chat_id, tz):
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE members SET timezone=? WHERE user_id=? AND chat_id=?",
                (tz, user_id, chat_id)
            )
            conn.commit()
        finally:
            conn.close()

    def get_leader_timezone(self, chat_id) -> Optional[str]:
        conn = self._conn()
        try:
            row = self._row(conn.execute("""
                SELECT m.timezone FROM members m
                JOIN groups g ON g.leader_id=m.user_id AND g.chat_id=m.chat_id
                WHERE g.chat_id=?
            """, (chat_id,)))
            return row["timezone"] if row else None
        finally:
            conn.close()

    # ── Member targets ────────────────────────────────────────────────────────────

    def set_member_target(self, user_id, chat_id, goal_type, target, target2=None, period="daily"):
        conn = self._conn()
        try:
            conn.execute("""
                INSERT INTO member_targets (user_id, chat_id, goal_type, target, target2, period)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(user_id, chat_id, goal_type) DO UPDATE SET
                    target=excluded.target, target2=excluded.target2, period=excluded.period
            """, (user_id, chat_id, goal_type, target, target2, period))
            conn.commit()
        finally:
            conn.close()

    def get_member_target(self, user_id, chat_id, goal_type) -> Optional[_Row]:
        conn = self._conn()
        try:
            return self._row(conn.execute(
                "SELECT * FROM member_targets WHERE user_id=? AND chat_id=? AND goal_type=?",
                (user_id, chat_id, goal_type)
            ))
        finally:
            conn.close()

    def get_all_member_targets(self, user_id, chat_id):
        conn = self._conn()
        try:
            return self._rows(conn.execute(
                "SELECT * FROM member_targets WHERE user_id=? AND chat_id=?",
                (user_id, chat_id)
            ))
        finally:
            conn.close()

    # ── Calorie logs ──────────────────────────────────────────────────────────────

    def log_calories(self, user_id, chat_id, log_date: date, calories: int, label=None):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO calorie_logs (user_id, chat_id, log_date, calories, label) VALUES (?,?,?,?,?)",
                (user_id, chat_id, str(log_date), calories, label)
            )
            conn.commit()
        finally:
            conn.close()

    def get_cal_day_entries(self, user_id, chat_id, log_date: date):
        conn = self._conn()
        try:
            return self._rows(conn.execute(
                "SELECT id, calories, label FROM calorie_logs WHERE user_id=? AND chat_id=? AND log_date=? ORDER BY logged_at",
                (user_id, chat_id, str(log_date))
            ))
        finally:
            conn.close()

    def delete_cal_entry(self, entry_id: int, user_id: int, chat_id: int):
        conn = self._conn()
        try:
            row = self._row(conn.execute(
                "SELECT log_date FROM calorie_logs WHERE id=? AND user_id=? AND chat_id=?",
                (entry_id, user_id, chat_id)
            ))
            if row:
                conn.execute("DELETE FROM calorie_logs WHERE id=?", (entry_id,))
                conn.commit()
            return row["log_date"] if row else None
        finally:
            conn.close()

    def get_cal_day_total(self, user_id, chat_id, log_date: date) -> int:
        conn = self._conn()
        try:
            row = self._row(conn.execute(
                "SELECT COALESCE(SUM(calories),0) as t FROM calorie_logs WHERE user_id=? AND chat_id=? AND log_date=?",
                (user_id, chat_id, str(log_date))
            ))
            return row["t"] if row else 0
        finally:
            conn.close()

    def get_cal_week_total(self, user_id, chat_id, week_start: date) -> int:
        conn = self._conn()
        try:
            row = self._row(conn.execute(
                "SELECT COALESCE(SUM(calories),0) as t FROM calorie_logs WHERE user_id=? AND chat_id=? AND log_date>=?",
                (user_id, chat_id, str(week_start))
            ))
            return row["t"] if row else 0
        finally:
            conn.close()

    def remove_cal_day(self, user_id, chat_id, log_date: date):
        conn = self._conn()
        try:
            conn.execute(
                "DELETE FROM calorie_logs WHERE user_id=? AND chat_id=? AND log_date=?",
                (user_id, chat_id, str(log_date))
            )
            conn.execute(
                "DELETE FROM penalty_log WHERE user_id=? AND chat_id=? AND goal_type='cal' AND period_key=?",
                (user_id, chat_id, f"daily_{log_date}")
            )
            conn.execute(
                "DELETE FROM debts WHERE user_id=? AND chat_id=? AND date=? AND reason LIKE 'Exceeded daily cal%'",
                (user_id, chat_id, str(log_date))
            )
            conn.commit()
        finally:
            conn.close()

    def get_cal_daily_totals_range(self, user_id, chat_id, start: date, end: date):
        conn = self._conn()
        try:
            return self._rows(conn.execute("""
                SELECT log_date, COALESCE(SUM(calories),0) as total
                FROM calorie_logs
                WHERE user_id=? AND chat_id=? AND log_date>=? AND log_date<=?
                GROUP BY log_date ORDER BY log_date
            """, (user_id, chat_id, str(start), str(end))))
        finally:
            conn.close()

    # ── Activity logs (run / walk) ────────────────────────────────────────────────

    def log_activity(self, user_id, chat_id, goal_type, log_date: date, value: float, label=None):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO activity_logs (user_id, chat_id, goal_type, log_date, value, label) VALUES (?,?,?,?,?,?)",
                (user_id, chat_id, goal_type, str(log_date), value, label)
            )
            conn.commit()
        finally:
            conn.close()

    def get_activity_day_entries(self, user_id, chat_id, goal_type, log_date: date):
        conn = self._conn()
        try:
            return self._rows(conn.execute(
                "SELECT id, value, label FROM activity_logs WHERE user_id=? AND chat_id=? AND goal_type=? AND log_date=? ORDER BY logged_at",
                (user_id, chat_id, goal_type, str(log_date))
            ))
        finally:
            conn.close()

    def delete_activity_entry(self, entry_id: int, user_id: int, chat_id: int):
        conn = self._conn()
        try:
            row = self._row(conn.execute(
                "SELECT log_date, goal_type FROM activity_logs WHERE id=? AND user_id=? AND chat_id=?",
                (entry_id, user_id, chat_id)
            ))
            if row:
                conn.execute("DELETE FROM activity_logs WHERE id=?", (entry_id,))
                conn.commit()
            return row if row else None
        finally:
            conn.close()

    def get_activity_day_total(self, user_id, chat_id, goal_type, log_date: date) -> float:
        conn = self._conn()
        try:
            row = self._row(conn.execute(
                "SELECT COALESCE(SUM(value),0) as t FROM activity_logs WHERE user_id=? AND chat_id=? AND goal_type=? AND log_date=?",
                (user_id, chat_id, goal_type, str(log_date))
            ))
            return row["t"] if row else 0.0
        finally:
            conn.close()

    def get_activity_week_total(self, user_id, chat_id, goal_type, week_start: date) -> float:
        conn = self._conn()
        try:
            row = self._row(conn.execute(
                "SELECT COALESCE(SUM(value),0) as t FROM activity_logs WHERE user_id=? AND chat_id=? AND goal_type=? AND log_date>=?",
                (user_id, chat_id, goal_type, str(week_start))
            ))
            return row["t"] if row else 0.0
        finally:
            conn.close()

    def get_activity_qualifying_days(self, user_id, chat_id, goal_type, week_start: date, min_value: float) -> int:
        conn = self._conn()
        try:
            row = self._row(conn.execute("""
                SELECT COUNT(*) as cnt FROM (
                    SELECT log_date, SUM(value) as day_total
                    FROM activity_logs
                    WHERE user_id=? AND chat_id=? AND goal_type=? AND log_date>=?
                    GROUP BY log_date
                    HAVING day_total >= ?
                )
            """, (user_id, chat_id, goal_type, str(week_start), min_value)))
            return row["cnt"] if row else 0
        finally:
            conn.close()

    def get_activity_daily_totals_range(self, user_id, chat_id, goal_type, start: date, end: date):
        conn = self._conn()
        try:
            return self._rows(conn.execute("""
                SELECT log_date, COALESCE(SUM(value),0) as total
                FROM activity_logs
                WHERE user_id=? AND chat_id=? AND goal_type=? AND log_date>=? AND log_date<=?
                GROUP BY log_date ORDER BY log_date
            """, (user_id, chat_id, goal_type, str(start), str(end))))
        finally:
            conn.close()

    def remove_activity_day(self, user_id, chat_id, goal_type, log_date: date):
        conn = self._conn()
        try:
            conn.execute(
                "DELETE FROM activity_logs WHERE user_id=? AND chat_id=? AND goal_type=? AND log_date=?",
                (user_id, chat_id, goal_type, str(log_date))
            )
            conn.commit()
        finally:
            conn.close()

    # ── Weight logs ───────────────────────────────────────────────────────────────

    def log_weight(self, user_id, chat_id, log_date: date, weight_kg: float):
        conn = self._conn()
        try:
            # Use INSERT OR REPLACE since we have a unique index on (user_id, chat_id, log_date)
            conn.execute("""
                INSERT INTO weight_logs (user_id, chat_id, log_date, weight_kg)
                VALUES (?,?,?,?)
                ON CONFLICT(user_id, chat_id, log_date) DO UPDATE SET
                    weight_kg=excluded.weight_kg, logged_at=CURRENT_TIMESTAMP
            """, (user_id, chat_id, str(log_date), weight_kg))
            conn.commit()
        finally:
            conn.close()

    def get_latest_weight(self, user_id, chat_id) -> Optional[float]:
        conn = self._conn()
        try:
            row = self._row(conn.execute(
                "SELECT weight_kg FROM weight_logs WHERE user_id=? AND chat_id=? ORDER BY log_date DESC LIMIT 1",
                (user_id, chat_id)
            ))
            return row["weight_kg"] if row else None
        finally:
            conn.close()

    def get_oldest_weight(self, user_id, chat_id) -> Optional[float]:
        conn = self._conn()
        try:
            row = self._row(conn.execute(
                "SELECT weight_kg FROM weight_logs WHERE user_id=? AND chat_id=? ORDER BY log_date ASC LIMIT 1",
                (user_id, chat_id)
            ))
            return row["weight_kg"] if row else None
        finally:
            conn.close()

    def get_weight_history(self, user_id, chat_id, limit=30):
        conn = self._conn()
        try:
            return self._rows(conn.execute(
                "SELECT log_date, weight_kg FROM weight_logs WHERE user_id=? AND chat_id=? ORDER BY log_date DESC LIMIT ?",
                (user_id, chat_id, limit)
            ))
        finally:
            conn.close()

    def get_weight_range(self, user_id, chat_id, start: date, end: date):
        conn = self._conn()
        try:
            return self._rows(conn.execute(
                "SELECT log_date, weight_kg FROM weight_logs WHERE user_id=? AND chat_id=? AND log_date>=? AND log_date<=? ORDER BY log_date",
                (user_id, chat_id, str(start), str(end))
            ))
        finally:
            conn.close()

    # ── Debts & payments ──────────────────────────────────────────────────────────

    def add_debt(self, user_id, chat_id, amount, reason):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO debts (user_id, chat_id, amount, reason, date) VALUES (?,?,?,?,?)",
                (user_id, chat_id, amount, reason, str(date.today()))
            )
            conn.commit()
        finally:
            conn.close()

    def get_total_debt(self, user_id, chat_id) -> float:
        conn = self._conn()
        try:
            charged = self._row(conn.execute(
                "SELECT COALESCE(SUM(amount),0) as t FROM debts WHERE user_id=? AND chat_id=?",
                (user_id, chat_id)
            ))["t"]
            paid = self._row(conn.execute(
                "SELECT COALESCE(SUM(amount),0) as t FROM payments WHERE user_id=? AND chat_id=?",
                (user_id, chat_id)
            ))["t"]
            return max(charged - paid, 0)
        finally:
            conn.close()

    def record_payment(self, user_id, chat_id, amount):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO payments (user_id, chat_id, amount, date) VALUES (?,?,?,?)",
                (user_id, chat_id, amount, str(date.today()))
            )
            conn.commit()
        finally:
            conn.close()

    def get_debt_history(self, user_id, chat_id):
        conn = self._conn()
        try:
            return self._rows(conn.execute(
                "SELECT * FROM debts WHERE user_id=? AND chat_id=? ORDER BY created_at",
                (user_id, chat_id)
            ))
        finally:
            conn.close()

    def get_payment_history(self, user_id, chat_id):
        conn = self._conn()
        try:
            return self._rows(conn.execute(
                "SELECT * FROM payments WHERE user_id=? AND chat_id=? ORDER BY created_at",
                (user_id, chat_id)
            ))
        finally:
            conn.close()

    def get_all_debts_for_group(self, chat_id):
        conn = self._conn()
        try:
            return self._rows(conn.execute("""
                SELECT m.username, m.user_id,
                       COALESCE(SUM(d.amount),0) - COALESCE(SUM(p.amount),0) as owing,
                       COALESCE(SUM(p.amount),0) as paid_total
                FROM members m
                LEFT JOIN debts d ON d.user_id=m.user_id AND d.chat_id=m.chat_id
                LEFT JOIN payments p ON p.user_id=m.user_id AND p.chat_id=m.chat_id
                WHERE m.chat_id=?
                GROUP BY m.user_id
            """, (chat_id,)))
        finally:
            conn.close()

    # ── Penalty tracking ──────────────────────────────────────────────────────────

    def penalty_issued(self, user_id, chat_id, goal_type, period_key: str) -> bool:
        conn = self._conn()
        try:
            row = self._row(conn.execute(
                "SELECT 1 as exists_flag FROM penalty_log WHERE user_id=? AND chat_id=? AND goal_type=? AND period_key=?",
                (user_id, chat_id, goal_type, period_key)
            ))
            return row is not None
        finally:
            conn.close()

    def mark_penalty(self, user_id, chat_id, goal_type, period_key: str):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO penalty_log (user_id, chat_id, goal_type, period_key) VALUES (?,?,?,?)",
                (user_id, chat_id, goal_type, period_key)
            )
            conn.commit()
        finally:
            conn.close()

    def unmark_penalty(self, user_id, chat_id, goal_type, period_key: str):
        conn = self._conn()
        try:
            conn.execute(
                "DELETE FROM penalty_log WHERE user_id=? AND chat_id=? AND goal_type=? AND period_key=?",
                (user_id, chat_id, goal_type, period_key)
            )
            conn.commit()
        finally:
            conn.close()

    def reverse_penalty_debt(self, user_id, chat_id, reason_fragment: str):
        conn = self._conn()
        try:
            row = self._row(conn.execute(
                "SELECT id FROM debts WHERE user_id=? AND chat_id=? AND reason LIKE ? ORDER BY created_at DESC LIMIT 1",
                (user_id, chat_id, f"%{reason_fragment}%")
            ))
            if row:
                conn.execute("DELETE FROM debts WHERE id=?", (row["id"],))
                conn.commit()
                return True
            return False
        finally:
            conn.close()

    def has_any_log_today(self, user_id, chat_id, goal_types: list, log_date: date) -> bool:
        conn = self._conn()
        try:
            if "cal" in goal_types:
                r = self._row(conn.execute(
                    "SELECT 1 as f FROM calorie_logs WHERE user_id=? AND chat_id=? AND log_date=? LIMIT 1",
                    (user_id, chat_id, str(log_date))
                ))
                if r:
                    return True
            activity_types = [g for g in goal_types if g in ("run", "walk")]
            if activity_types:
                placeholders = ",".join("?" * len(activity_types))
                r = self._row(conn.execute(
                    f"SELECT 1 as f FROM activity_logs WHERE user_id=? AND chat_id=? AND log_date=? AND goal_type IN ({placeholders}) LIMIT 1",
                    (user_id, chat_id, str(log_date), *activity_types)
                ))
                if r:
                    return True
            return False
        finally:
            conn.close()
