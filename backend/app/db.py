import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def _init(self):
        with self.connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_config (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pairing (
                    code TEXT PRIMARY KEY,
                    expires_at INTEGER NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS allowed_groups (
                    chat_id TEXT PRIMARY KEY,
                    title TEXT,
                    added_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_updates (
                    update_id TEXT PRIMARY KEY,
                    received_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mistral_conversations (
                    chat_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspace_leases (
                    chat_id TEXT PRIMARY KEY,
                    workspace_token TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    access_mode TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    mcp_session_id TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                );
                """
            )
            try:
                con.execute("ALTER TABLE workspace_leases ADD COLUMN mcp_session_id TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
                        # Known v0.2 routing tables are obsolete in the standalone Mistral architecture.
            con.execute("DROP TABLE IF EXISTS chat_routes")
            con.execute("DROP TABLE IF EXISTS web_handoffs")

    def get_config(self) -> dict[str, Any]:
        with self.connect() as con:
            rows = con.execute("SELECT key,value_json FROM app_config").fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value_json"])
            except Exception:
                continue
        return result

    def set_config(self, values: dict[str, Any]) -> None:
        now = int(time.time())
        with self.connect() as con:
            for key, value in values.items():
                con.execute(
                    "INSERT INTO app_config(key,value_json,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                    (key, json.dumps(value, ensure_ascii=False), now),
                )

    def clear_config(self) -> None:
        with self.connect() as con:
            con.execute("DELETE FROM app_config")

    def get_state(self, key: str) -> str | None:
        with self.connect() as con:
            row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def set_state(self, key: str, value: str):
        with self.connect() as con:
            con.execute(
                "INSERT INTO state(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def delete_state(self, key: str):
        with self.connect() as con:
            con.execute("DELETE FROM state WHERE key=?", (key,))

    def new_pairing_code(self, ttl_seconds: int = 600) -> str:
        code = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
        with self.connect() as con:
            con.execute("DELETE FROM pairing WHERE expires_at < ? OR used=1", (int(time.time()),))
            con.execute(
                "INSERT INTO pairing(code,expires_at,used) VALUES(?,?,0)",
                (code, int(time.time()) + ttl_seconds),
            )
        return code

    def consume_pairing_code(self, code: str) -> bool:
        now = int(time.time())
        with self.connect() as con:
            row = con.execute(
                "SELECT code FROM pairing WHERE code=? AND used=0 AND expires_at>=?",
                (code, now),
            ).fetchone()
            if not row:
                return False
            con.execute("UPDATE pairing SET used=1 WHERE code=?", (code,))
            return True

    def allow_group(self, chat_id: str, title: str = ""):
        with self.connect() as con:
            con.execute(
                "INSERT INTO allowed_groups(chat_id,title,added_at) VALUES(?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,added_at=excluded.added_at",
                (chat_id, title, int(time.time())),
            )

    def disallow_group(self, chat_id: str):
        with self.connect() as con:
            con.execute("DELETE FROM allowed_groups WHERE chat_id=?", (chat_id,))

    def group_allowed(self, chat_id: str) -> bool:
        with self.connect() as con:
            return con.execute("SELECT 1 FROM allowed_groups WHERE chat_id=?", (chat_id,)).fetchone() is not None

    def list_allowed_groups(self) -> list[dict]:
        with self.connect() as con:
            rows = con.execute("SELECT chat_id,title,added_at FROM allowed_groups ORDER BY added_at DESC").fetchall()
            return [dict(row) for row in rows]

    def claim_telegram_update(self, update_id: int | str, retention_seconds: int = 86400) -> bool:
        now = int(time.time())
        with self.connect() as con:
            con.execute("DELETE FROM telegram_updates WHERE received_at < ?", (now - retention_seconds,))
            cur = con.execute(
                "INSERT OR IGNORE INTO telegram_updates(update_id,received_at) VALUES(?,?)",
                (str(update_id), now),
            )
            return cur.rowcount == 1

    def set_mistral_conversation(self, chat_id: str, conversation_id: str):
        with self.connect() as con:
            con.execute(
                "INSERT INTO mistral_conversations(chat_id,conversation_id,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET conversation_id=excluded.conversation_id,updated_at=excluded.updated_at",
                (chat_id, conversation_id, int(time.time())),
            )

    def get_mistral_conversation(self, chat_id: str) -> str | None:
        with self.connect() as con:
            row = con.execute("SELECT conversation_id FROM mistral_conversations WHERE chat_id=?", (chat_id,)).fetchone()
            return row["conversation_id"] if row else None

    def clear_mistral_conversation(self, chat_id: str):
        with self.connect() as con:
            con.execute("DELETE FROM mistral_conversations WHERE chat_id=?", (chat_id,))

    def clear_all_mistral_conversations(self):
        with self.connect() as con:
            con.execute("DELETE FROM mistral_conversations")

    def count_mistral_conversations(self) -> int:
        with self.connect() as con:
            row = con.execute("SELECT COUNT(*) AS n FROM mistral_conversations").fetchone()
            return int(row["n"])
    def set_workspace_lease(self, chat_id: str, workspace_token: str, lease_id: str, access_mode: str, capabilities: list[str] | tuple[str, ...], mcp_session_id: str = ""):
        with self.connect() as con:
            con.execute(
                "INSERT INTO workspace_leases(chat_id,workspace_token,lease_id,access_mode,capabilities_json,mcp_session_id,updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET workspace_token=excluded.workspace_token,lease_id=excluded.lease_id,access_mode=excluded.access_mode,capabilities_json=excluded.capabilities_json,mcp_session_id=excluded.mcp_session_id,updated_at=excluded.updated_at",
                (str(chat_id), str(workspace_token), str(lease_id), str(access_mode), json.dumps(list(capabilities)), str(mcp_session_id), int(time.time())),
            )

    def get_workspace_lease(self, chat_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT workspace_token,lease_id,access_mode,capabilities_json,mcp_session_id,updated_at FROM workspace_leases WHERE chat_id=?",
                (str(chat_id),),
            ).fetchone()
        if not row:
            return None
        try:
            caps = json.loads(row["capabilities_json"])
        except Exception:
            caps = []
        return {
            "workspace_token": row["workspace_token"],
            "lease_id": row["lease_id"],
            "access_mode": row["access_mode"],
            "capabilities": caps if isinstance(caps, list) else [],
            "mcp_session_id": row["mcp_session_id"],
            "updated_at": row["updated_at"],
        }

    def clear_workspace_lease(self, chat_id: str):
        with self.connect() as con:
            con.execute("DELETE FROM workspace_leases WHERE chat_id=?", (str(chat_id),))

    def list_workspace_leases(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT chat_id,workspace_token,lease_id,access_mode,capabilities_json,mcp_session_id,updated_at FROM workspace_leases").fetchall()
        out = []
        for row in rows:
            try:
                caps = json.loads(row["capabilities_json"])
            except Exception:
                caps = []
            out.append({"chat_id": row["chat_id"], "workspace_token": row["workspace_token"], "lease_id": row["lease_id"], "access_mode": row["access_mode"], "capabilities": caps if isinstance(caps,list) else [], "mcp_session_id": row["mcp_session_id"], "updated_at": row["updated_at"]})
        return out
