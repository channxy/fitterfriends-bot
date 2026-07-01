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
        self._connection = None
        self._init()

    def _conn(self):
        """Return the persistent connection, creating it once."""
        if self._connection is not None:
            return self._connection
        if _BACKEND == "libsql":
            if self._token and self.url.startswith("libsql://"):
                # Embedded replica: local cache synced to Turso
                conn = libsql.connect("/tmp/ff_replica.db",
                                      sync_url=self.url,
                                      auth_token=self._token)
                conn.sync()
            else:
                local = self.url if not self.url.startswith("libsql://") else "fitterfriends.db"
                conn = libsql.connect(local)
        else:
            local = self.url if not self.url.startswith("libsql://") else "fitterfriends.db"
            conn = libsql.connect(local)
            conn.row_factory = None
        self._connection = conn
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
            """CREATE TABLE IF NOT EXISTS challenges (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                name       TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date   TEXT,
                status     TEXT NOT NULL DEFAULT 'active'
            )""",
            """CREATE TABLE IF NOT EXISTS calorie_logs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                chat_id      INTEGER NOT NULL,
                challenge_id INTEGER,
                log_date     TEXT NOT NULL,
                calories     INTEGER NOT NULL,
                label        TEXT,
                logged_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS activity_logs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                chat_id      INTEGER NOT NULL,
                challenge_id INTEGER,
                goal_type    TEXT NOT NULL,
                log_date     TEXT NOT NULL,
                value        REAL NOT NULL,
                label        TEXT,
                logged_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS weight_logs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                chat_id      INTEGER NOT NULL,
                challenge_id INTEGER,
                log_date     TEXT NOT NULL,
                weight_kg    REAL NOT NULL,
                logged_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS debts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                chat_id      INTEGER NOT NULL,
                challenge_id INTEGER,
                amount       REAL NOT NULL,
                reason       TEXT,
                date         TEXT NOT NULL,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_weight_unique
               ON weight_logs (user_id, chat_id, log_date)""",
        ]
        for stmt in statements:
            conn.execute(stmt)
        # Migrate existing tables — add challenge_id if not present
        for table in ("calorie_logs", "activity_logs", "weight_logs", "debts"):
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN challenge_id INTEGER")
            except Exception:
                pass  # column already exists
        conn.commit()

    # ── Challenges ────────────────────────────────────────────────────────────────

    def create_challenge(self, chat_id: int, name: str, start_date: date,
                         end_date=None) -> int:
        conn = self._conn()
        # End any currently active challenge first
        conn.execute(
                "UPDATE challenges SET status='ended' WHERE chat_id=? AND status='active'",
                (chat_id,)
        )
        cur = conn.execute(
                "INSERT INTO challenges (chat_id, name, start_date, end_date, status) VALUES (?,?,?,?,'active')",
                (chat_id, name, str(start_date), str(end_date) if end_date else None)
        )
        conn.commit()
        # Fetch the new id
        row = self._row(conn.execute(
                "SELECT id FROM challenges WHERE chat_id=? AND status='active'", (chat_id,)
        ))
        return row["id"] if row else 0

    def get_active_challenge(self, chat_id: int) -> Optional[_Row]:
        conn = self._conn()
        return self._row(conn.execute(
                "SELECT * FROM challenges WHERE chat_id=? AND status='active' LIMIT 1",
                (chat_id,)
        ))

    def get_challenge(self, challenge_id: int) -> Optional[_Row]:
        conn = self._conn()
        return self._row(conn.execute(
                "SELECT * FROM challenges WHERE id=?", (challenge_id,)
        ))

    def end_challenge(self, challenge_id: int):
        conn = self._conn()
        conn.execute(
                "UPDATE challenges SET status='ended' WHERE id=?", (challenge_id,)
        )
        conn.commit()

    def get_challenge_history(self, chat_id: int):
        conn = self._conn()
        return self._rows(conn.execute(
                "SELECT * FROM challenges WHERE chat_id=? ORDER BY start_date DESC",
                (chat_id,)
        ))

    def get_all_active_challenges(self):
        conn = self._conn()
        return self._rows(conn.execute(
                "SELECT * FROM challenges WHERE status='active'"
        ))

    def get_challenge_member_stats(self, challenge_id: int, chat_id: int):
        """Per-member compliance stats for a challenge."""
        conn = self._conn()
        members = self._rows(conn.execute(
                "SELECT user_id, username FROM members WHERE chat_id=?", (chat_id,)
        ))
        challenge = self._row(conn.execute(
                "SELECT * FROM challenges WHERE id=?", (challenge_id,)
        ))
        if not challenge:
            return []
        results = []
        for m in members:
            uid = m["user_id"]
            debt_row = self._row(conn.execute(
                "SELECT COALESCE(SUM(amount),0) as t FROM debts WHERE user_id=? AND chat_id=? AND challenge_id=?",
                (uid, chat_id, challenge_id)
            ))
            cal_days = self._row(conn.execute(
                "SELECT COUNT(DISTINCT log_date) as c FROM calorie_logs WHERE user_id=? AND chat_id=? AND challenge_id=?",
                (uid, chat_id, challenge_id)
            ))
            results.append({
                "user_id": uid,
                "username": m["username"],
                "debt": debt_row["t"] if debt_row else 0,
                "days_logged": cal_days["c"] if cal_days else 0,
            })
        return results

    # ── Groups ────────────────────────────────────────────────────────────────────

    def save_group(self, chat_id, leader_id, reset_day, payment_mode, payment_target):
        conn = self._conn()
        conn.execute("""
                INSERT INTO groups (chat_id, leader_id, reset_day, payment_mode, payment_target)
                VALUES (?,?,?,?,?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    leader_id=excluded.leader_id, reset_day=excluded.reset_day,
                    payment_mode=excluded.payment_mode, payment_target=excluded.payment_target
        """, (chat_id, leader_id, reset_day, payment_mode, payment_target))
        conn.commit()

    def get_group(self, chat_id) -> Optional[_Row]:
        conn = self._conn()
        return self._row(conn.execute("SELECT * FROM groups WHERE chat_id=?", (chat_id,)))

    def get_all_groups(self):
        conn = self._conn()
        return self._rows(conn.execute("SELECT * FROM groups"))

    # ── Group goals ───────────────────────────────────────────────────────────────

    def save_group_goal(self, chat_id, goal_type, daily_penalty, weekly_penalty, run_unit="km"):
        conn = self._conn()
        conn.execute("""
                INSERT INTO group_goals (chat_id, goal_type, daily_penalty, weekly_penalty, run_unit)
                VALUES (?,?,?,?,?)
                ON CONFLICT(chat_id, goal_type) DO UPDATE SET
                    daily_penalty=excluded.daily_penalty,
                    weekly_penalty=excluded.weekly_penalty,
                    run_unit=excluded.run_unit
        """, (chat_id, goal_type, daily_penalty, weekly_penalty, run_unit))
        conn.commit()

    def get_group_goals(self, chat_id):
        conn = self._conn()
        return self._rows(conn.execute(
                "SELECT * FROM group_goals WHERE chat_id=?", (chat_id,)
        ))

    def get_group_goal(self, chat_id, goal_type) -> Optional[_Row]:
        conn = self._conn()
        return self._row(conn.execute(
                "SELECT * FROM group_goals WHERE chat_id=? AND goal_type=?",
                (chat_id, goal_type)
        ))

    def delete_group_goals(self, chat_id):
        conn = self._conn()
        conn.execute("DELETE FROM group_goals WHERE chat_id=?", (chat_id,))
        conn.commit()

    # ── Members ───────────────────────────────────────────────────────────────────

    def ensure_member(self, user_id, chat_id, username):
        conn = self._conn()
        conn.execute("""
                INSERT INTO members (user_id, chat_id, username)
                VALUES (?,?,?)
                ON CONFLICT(user_id, chat_id) DO UPDATE SET username=excluded.username
        """, (user_id, chat_id, username))
        conn.commit()

    def get_member(self, user_id, chat_id) -> Optional[_Row]:
        conn = self._conn()
        return self._row(conn.execute(
                "SELECT * FROM members WHERE user_id=? AND chat_id=?", (user_id, chat_id)
        ))

    def get_all_members(self, chat_id):
        conn = self._conn()
        return self._rows(conn.execute("SELECT * FROM members WHERE chat_id=?", (chat_id,)))

    def set_member_timezone(self, user_id, chat_id, tz):
        conn = self._conn()
        conn.execute(
                "UPDATE members SET timezone=? WHERE user_id=? AND chat_id=?",
                (tz, user_id, chat_id)
        )
        conn.commit()

    def get_leader_timezone(self, chat_id) -> Optional[str]:
        conn = self._conn()
        row = self._row(conn.execute("""
                SELECT m.timezone FROM members m
                JOIN groups g ON g.leader_id=m.user_id AND g.chat_id=m.chat_id
                WHERE g.chat_id=?
        """, (chat_id,)))
        return row["timezone"] if row else None

    # ── Member targets ────────────────────────────────────────────────────────────

    def set_member_target(self, user_id, chat_id, goal_type, target, target2=None, period="daily"):
        conn = self._conn()
        conn.execute("""
                INSERT INTO member_targets (user_id, chat_id, goal_type, target, target2, period)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(user_id, chat_id, goal_type) DO UPDATE SET
                    target=excluded.target, target2=excluded.target2, period=excluded.period
        """, (user_id, chat_id, goal_type, target, target2, period))
        conn.commit()

    def get_member_target(self, user_id, chat_id, goal_type) -> Optional[_Row]:
        conn = self._conn()
        return self._row(conn.execute(
                "SELECT * FROM member_targets WHERE user_id=? AND chat_id=? AND goal_type=?",
                (user_id, chat_id, goal_type)
        ))

    def get_all_member_targets(self, user_id, chat_id):
        conn = self._conn()
        return self._rows(conn.execute(
                "SELECT * FROM member_targets WHERE user_id=? AND chat_id=?",
                (user_id, chat_id)
        ))

    # ── Calorie logs ──────────────────────────────────────────────────────────────

    def log_calories(self, user_id, chat_id, log_date: date, calories: int, label=None):
        challenge = self.get_active_challenge(chat_id)
        cid = challenge["id"] if challenge else None
        conn = self._conn()
        conn.execute(
                "INSERT INTO calorie_logs (user_id, chat_id, challenge_id, log_date, calories, label) VALUES (?,?,?,?,?,?)",
                (user_id, chat_id, cid, str(log_date), calories, label)
        )
        conn.commit()

    def get_cal_day_entries(self, user_id, chat_id, log_date: date):
        conn = self._conn()
        return self._rows(conn.execute(
                "SELECT id, calories, label FROM calorie_logs WHERE user_id=? AND chat_id=? AND log_date=? ORDER BY logged_at",
                (user_id, chat_id, str(log_date))
        ))

    def delete_cal_entry(self, entry_id: int, user_id: int, chat_id: int):
        conn = self._conn()
        row = self._row(conn.execute(
                "SELECT log_date FROM calorie_logs WHERE id=? AND user_id=? AND chat_id=?",
                (entry_id, user_id, chat_id)
        ))
        if row:
                conn.execute("DELETE FROM calorie_logs WHERE id=?", (entry_id,))
                conn.commit()
        return row["log_date"] if row else None

    def get_cal_day_total(self, user_id, chat_id, log_date: date) -> int:
        conn = self._conn()
        row = self._row(conn.execute(
                "SELECT COALESCE(SUM(calories),0) as t FROM calorie_logs WHERE user_id=? AND chat_id=? AND log_date=?",
                (user_id, chat_id, str(log_date))
        ))
        return row["t"] if row else 0

    def get_cal_week_total(self, user_id, chat_id, week_start: date) -> int:
        conn = self._conn()
        row = self._row(conn.execute(
                "SELECT COALESCE(SUM(calories),0) as t FROM calorie_logs WHERE user_id=? AND chat_id=? AND log_date>=?",
                (user_id, chat_id, str(week_start))
        ))
        return row["t"] if row else 0

    def remove_cal_day(self, user_id, chat_id, log_date: date):
        conn = self._conn()
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

    def get_cal_daily_totals_range(self, user_id, chat_id, start: date, end: date):
        conn = self._conn()
        return self._rows(conn.execute("""
                SELECT log_date, COALESCE(SUM(calories),0) as total
                FROM calorie_logs
                WHERE user_id=? AND chat_id=? AND log_date>=? AND log_date<=?
                GROUP BY log_date ORDER BY log_date
        """, (user_id, chat_id, str(start), str(end))))

    # ── Activity logs (run / walk) ────────────────────────────────────────────────

    def log_activity(self, user_id, chat_id, goal_type, log_date: date, value: float, label=None):
        challenge = self.get_active_challenge(chat_id)
        cid = challenge["id"] if challenge else None
        conn = self._conn()
        conn.execute(
                "INSERT INTO activity_logs (user_id, chat_id, challenge_id, goal_type, log_date, value, label) VALUES (?,?,?,?,?,?,?)",
                (user_id, chat_id, cid, goal_type, str(log_date), value, label)
        )
        conn.commit()

    def get_activity_day_entries(self, user_id, chat_id, goal_type, log_date: date):
        conn = self._conn()
        return self._rows(conn.execute(
                "SELECT id, value, label FROM activity_logs WHERE user_id=? AND chat_id=? AND goal_type=? AND log_date=? ORDER BY logged_at",
                (user_id, chat_id, goal_type, str(log_date))
        ))

    def delete_activity_entry(self, entry_id: int, user_id: int, chat_id: int):
        conn = self._conn()
        row = self._row(conn.execute(
                "SELECT log_date, goal_type FROM activity_logs WHERE id=? AND user_id=? AND chat_id=?",
                (entry_id, user_id, chat_id)
        ))
        if row:
                conn.execute("DELETE FROM activity_logs WHERE id=?", (entry_id,))
                conn.commit()
        return row if row else None

    def get_activity_day_total(self, user_id, chat_id, goal_type, log_date: date) -> float:
        conn = self._conn()
        row = self._row(conn.execute(
                "SELECT COALESCE(SUM(value),0) as t FROM activity_logs WHERE user_id=? AND chat_id=? AND goal_type=? AND log_date=?",
                (user_id, chat_id, goal_type, str(log_date))
        ))
        return row["t"] if row else 0.0

    def get_activity_week_total(self, user_id, chat_id, goal_type, week_start: date) -> float:
        conn = self._conn()
        row = self._row(conn.execute(
                "SELECT COALESCE(SUM(value),0) as t FROM activity_logs WHERE user_id=? AND chat_id=? AND goal_type=? AND log_date>=?",
                (user_id, chat_id, goal_type, str(week_start))
        ))
        return row["t"] if row else 0.0

    def get_activity_qualifying_days(self, user_id, chat_id, goal_type, week_start: date, min_value: float) -> int:
        conn = self._conn()
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

    def get_activity_daily_totals_range(self, user_id, chat_id, goal_type, start: date, end: date):
        conn = self._conn()
        return self._rows(conn.execute("""
                SELECT log_date, COALESCE(SUM(value),0) as total
                FROM activity_logs
                WHERE user_id=? AND chat_id=? AND goal_type=? AND log_date>=? AND log_date<=?
                GROUP BY log_date ORDER BY log_date
        """, (user_id, chat_id, goal_type, str(start), str(end))))

    def remove_activity_day(self, user_id, chat_id, goal_type, log_date: date):
        conn = self._conn()
        conn.execute(
                "DELETE FROM activity_logs WHERE user_id=? AND chat_id=? AND goal_type=? AND log_date=?",
                (user_id, chat_id, goal_type, str(log_date))
        )
        conn.commit()

    # ── Weight logs ───────────────────────────────────────────────────────────────

    def log_weight(self, user_id, chat_id, log_date: date, weight_kg: float):
        challenge = self.get_active_challenge(chat_id)
        cid = challenge["id"] if challenge else None
        conn = self._conn()
        conn.execute("""
                INSERT INTO weight_logs (user_id, chat_id, challenge_id, log_date, weight_kg)
                VALUES (?,?,?,?,?)
                ON CONFLICT(user_id, chat_id, log_date) DO UPDATE SET
                    weight_kg=excluded.weight_kg, logged_at=CURRENT_TIMESTAMP
        """, (user_id, chat_id, cid, str(log_date), weight_kg))
        conn.commit()

    def get_latest_weight(self, user_id, chat_id) -> Optional[float]:
        conn = self._conn()
        row = self._row(conn.execute(
                "SELECT weight_kg FROM weight_logs WHERE user_id=? AND chat_id=? ORDER BY log_date DESC LIMIT 1",
                (user_id, chat_id)
        ))
        return row["weight_kg"] if row else None

    def get_oldest_weight(self, user_id, chat_id) -> Optional[float]:
        conn = self._conn()
        row = self._row(conn.execute(
                "SELECT weight_kg FROM weight_logs WHERE user_id=? AND chat_id=? ORDER BY log_date ASC LIMIT 1",
                (user_id, chat_id)
        ))
        return row["weight_kg"] if row else None

    def get_weight_history(self, user_id, chat_id, limit=30):
        conn = self._conn()
        return self._rows(conn.execute(
                "SELECT log_date, weight_kg FROM weight_logs WHERE user_id=? AND chat_id=? ORDER BY log_date DESC LIMIT ?",
                (user_id, chat_id, limit)
        ))

    def get_weight_range(self, user_id, chat_id, start: date, end: date):
        conn = self._conn()
        return self._rows(conn.execute(
                "SELECT log_date, weight_kg FROM weight_logs WHERE user_id=? AND chat_id=? AND log_date>=? AND log_date<=? ORDER BY log_date",
                (user_id, chat_id, str(start), str(end))
        ))

    # ── Debts & payments ──────────────────────────────────────────────────────────

    def add_debt(self, user_id, chat_id, amount, reason):
        challenge = self.get_active_challenge(chat_id)
        cid = challenge["id"] if challenge else None
        conn = self._conn()
        conn.execute(
                "INSERT INTO debts (user_id, chat_id, challenge_id, amount, reason, date) VALUES (?,?,?,?,?,?)",
                (user_id, chat_id, cid, amount, reason, str(date.today()))
        )
        conn.commit()

    def get_total_debt(self, user_id, chat_id) -> float:
        conn = self._conn()
        charged = self._row(conn.execute(
                "SELECT COALESCE(SUM(amount),0) as t FROM debts WHERE user_id=? AND chat_id=?",
                (user_id, chat_id)
        ))["t"]
        paid = self._row(conn.execute(
                "SELECT COALESCE(SUM(amount),0) as t FROM payments WHERE user_id=? AND chat_id=?",
                (user_id, chat_id)
        ))["t"]
        return max(charged - paid, 0)

    def record_payment(self, user_id, chat_id, amount):
        conn = self._conn()
        conn.execute(
                "INSERT INTO payments (user_id, chat_id, amount, date) VALUES (?,?,?,?)",
                (user_id, chat_id, amount, str(date.today()))
        )
        conn.commit()

    def get_debt_history(self, user_id, chat_id):
        conn = self._conn()
        return self._rows(conn.execute(
                "SELECT * FROM debts WHERE user_id=? AND chat_id=? ORDER BY created_at",
                (user_id, chat_id)
        ))

    def get_payment_history(self, user_id, chat_id):
        conn = self._conn()
        return self._rows(conn.execute(
                "SELECT * FROM payments WHERE user_id=? AND chat_id=? ORDER BY created_at",
                (user_id, chat_id)
        ))

    def get_all_debts_for_group(self, chat_id):
        conn = self._conn()
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

    # ── Penalty tracking ──────────────────────────────────────────────────────────

    def penalty_issued(self, user_id, chat_id, goal_type, period_key: str) -> bool:
        conn = self._conn()
        row = self._row(conn.execute(
                "SELECT 1 as exists_flag FROM penalty_log WHERE user_id=? AND chat_id=? AND goal_type=? AND period_key=?",
                (user_id, chat_id, goal_type, period_key)
        ))
        return row is not None

    def mark_penalty(self, user_id, chat_id, goal_type, period_key: str):
        conn = self._conn()
        conn.execute(
                "INSERT OR IGNORE INTO penalty_log (user_id, chat_id, goal_type, period_key) VALUES (?,?,?,?)",
                (user_id, chat_id, goal_type, period_key)
        )
        conn.commit()

    def unmark_penalty(self, user_id, chat_id, goal_type, period_key: str):
        conn = self._conn()
        conn.execute(
                "DELETE FROM penalty_log WHERE user_id=? AND chat_id=? AND goal_type=? AND period_key=?",
                (user_id, chat_id, goal_type, period_key)
        )
        conn.commit()

    def reverse_penalty_debt(self, user_id, chat_id, reason_fragment: str):
        conn = self._conn()
        row = self._row(conn.execute(
                "SELECT id FROM debts WHERE user_id=? AND chat_id=? AND reason LIKE ? ORDER BY created_at DESC LIMIT 1",
                (user_id, chat_id, f"%{reason_fragment}%")
        ))
        if row:
                conn.execute("DELETE FROM debts WHERE id=?", (row["id"],))
                conn.commit()
                return True
        return False

    def has_any_log_today(self, user_id, chat_id, goal_types: list, log_date: date) -> bool:
        conn = self._conn()
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
