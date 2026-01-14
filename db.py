import sqlite3
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                is_premium INTEGER DEFAULT 0,
                credits INTEGER DEFAULT 0,
                validity_expire_at TEXT,
                selected_model TEXT,
                tts_speed TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS voices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                file_path TEXT,
                created_at TEXT
            )
            """
        )

        # Migration safety: if old DB exists without tts_speed, add it.
        try:
            cur.execute("ALTER TABLE users ADD COLUMN tts_speed TEXT")
        except Exception:
            pass

        self.conn.commit()

    def ensure_user(self, user_id: int, username: Optional[str]):
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        now = datetime.utcnow().isoformat()
        if not row:
            cur.execute(
                "INSERT INTO users (id, username, is_premium, credits, tts_speed, created_at, updated_at) VALUES (?, ?, 0, 0, ?, ?, ?)",
                (user_id, username, "natural", now, now),
            )
            self.conn.commit()

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def update_user_fields(self, user_id: int, fields: Dict[str, Any]):
        if not fields:
            return
        fields["updated_at"] = datetime.utcnow().isoformat()
        keys = list(fields.keys())
        values = [fields[k] for k in keys]
        set_clause = ", ".join([f"{k} = ?" for k in keys])
        cur = self.conn.cursor()
        cur.execute(f"UPDATE users SET {set_clause} WHERE id = ?", (*values, user_id))
        self.conn.commit()

    def add_credits(self, user_id: int, amount: int):
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE users SET credits = COALESCE(credits,0) + ?, is_premium = 1, updated_at = ? WHERE id = ?",
            (amount, datetime.utcnow().isoformat(), user_id),
        )
        self.conn.commit()

    def remove_credits(self, user_id: int, amount: int):
        user = self.get_user(user_id)
        if not user:
            return
        new_credits = max(0, int(user.get("credits") or 0) - amount)
        is_premium = 1 if new_credits > 0 and self.is_valid(user_id) else 0
        self.update_user_fields(user_id, {"credits": new_credits, "is_premium": is_premium})

    def set_validity(self, user_id: int, days: int):
        expire_at = (datetime.utcnow() + timedelta(days=days)).isoformat()
        user = self.get_user(user_id)
        is_premium = 1 if (user and (user.get("credits") or 0) > 0) else 0
        self.update_user_fields(user_id, {"validity_expire_at": expire_at, "is_premium": is_premium})

    def remove_validity(self, user_id: int):
        self.update_user_fields(user_id, {"validity_expire_at": None, "is_premium": 0})

    def is_valid(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        if not user:
            return False
        exp = user.get("validity_expire_at")
        if not exp:
            return False
        try:
            return datetime.fromisoformat(exp) > datetime.utcnow()
        except Exception:
            return False

    def list_users(self, limit: int = 100) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
        return [dict(r) for r in rows]

    def list_all_users(self) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM users ORDER BY created_at DESC")
        rows = cur.fetchall()
        return [dict(r) for r in rows]

    def list_premium_users(self, limit: int = 100) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM users WHERE is_premium = 1 ORDER BY updated_at DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
        return [dict(r) for r in rows]

    def store_voice(self, user_id: int, file_path: str):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO voices (user_id, file_path, created_at) VALUES (?, ?, ?)",
            (user_id, file_path, datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    def list_user_voices(self, user_id: int) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM voices WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
        rows = cur.fetchall()
        return [dict(r) for r in rows]

    def delete_user_voices(self, user_id: int):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM voices WHERE user_id = ?", (user_id,))
        self.conn.commit()

    def get_admins(self) -> List[int]:
        cur = self.conn.cursor()
        cur.execute("SELECT user_id FROM admins")
        rows = cur.fetchall()
        return [int(r[0]) for r in rows]

    def add_admin(self, user_id: int):
        cur = self.conn.cursor()
        cur.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
        self.conn.commit()

    def remove_admin(self, user_id: int):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        self.conn.commit()

    def is_admin(self, user_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,))
        return cur.fetchone() is not None
