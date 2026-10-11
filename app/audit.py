"""SQLite-backed persistence for tickets, approvals, events, snapshots and operations."""

import json
import re

import sqlite3
import hashlib
import os

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List

from .models import now_iso
from .errors import StorageError

def mask_pii(value: str) -> str:
    """Mask common email addresses and mainland mobile numbers."""

    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "***@***", value)

    return re.sub(r"(?<!\d)1\d{10}(?!\d)", "1**********", value)

class AuditStore:
    """保存工单、审批和节点事件，确保程序重启后仍可查询。"""

    def __init__(self, db_path: Path, timeout: float = 5):
        """Open or initialize the configured SQLite database."""
        self.db_path = Path(db_path)
        self.timeout = timeout

        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with self._db() as conn:

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT NOT NULL,
                    node TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL, response TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS states (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                """
            )
            conn.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(operations)")}
            for name in ("ticket_id", "kind"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE operations ADD COLUMN {name} TEXT")

    def _connect(self) -> sqlite3.Connection:
        """打开一个新数据库连接；文件不存在时 SQLite 会创建它。"""

        return sqlite3.connect(self.db_path, timeout=self.timeout)

    def once(self, key, payload, operation, *, precondition=None, ticket_id=None, kind="workflow", initial_state=None,
             checkpoint=None):
        """跨进程原子占位；不明结果不自动重试，以避免重复副作用。"""
        import hashlib
        from .reliability import IdempotencyConflict, OperationPending
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT fingerprint,status,response FROM operations WHERE key=?", (key,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise IdempotencyConflict("同一幂等键不能用于不同请求")
                if row[1] != "completed":
                    raise OperationPending("请求正在处理或结果不明，请核查审计记录，勿更换键重复执行")
                return json.loads(row[2])
            if precondition is not None:
                precondition()
            conn.execute("INSERT INTO operations(key,fingerprint,status,response,created_at,ticket_id,kind) VALUES (?, ?, 'pending', NULL, ?, ?, ?)",
                         (key, fingerprint, now_iso(), ticket_id, kind))
            if initial_state is not None:
                self._write_snapshot(conn, initial_state)
            if checkpoint is not None:
                self._write_checkpoint(conn, **checkpoint)
        result = operation()
        with self._db() as conn:
            conn.execute("UPDATE operations SET status='completed',response=? WHERE key=?", (json.dumps(result, ensure_ascii=False), key))
        return result

    @staticmethod
    def _write_snapshot(conn, state):
        ticket = state["ticket"]
        conn.execute("INSERT OR REPLACE INTO tickets VALUES (?,?,?)",
                     (ticket["id"], json.dumps(ticket, ensure_ascii=False), ticket["updated_at"]))
        conn.execute("INSERT OR REPLACE INTO states VALUES (?,?)", (ticket["id"], json.dumps(state, ensure_ascii=False)))
        if state.get("approval"):
            approval = state["approval"]
            conn.execute("INSERT OR REPLACE INTO approvals VALUES (?,?,?)",
                         (approval["id"], ticket["id"], json.dumps(approval, ensure_ascii=False)))

    def save_checkpoint(self, state, node, detail=None, *, reconcile=False, resolve_tools=False):
        """节点业务、审批、快照、审计同一事务提交；失败全部回滚。"""
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._write_checkpoint(conn, state, node, detail, reconcile=reconcile, resolve_tools=resolve_tools)

    def _write_checkpoint(self, conn, state, node, detail=None, *, reconcile=False, resolve_tools=False):
        """也用于将请求占位与输入/审批决定一起提交，消除二者之间的崩溃窗口。"""
        self._write_snapshot(conn, state)
        payload = detail or {"status": state["ticket"]["status"], "result": state["result"],
                             "decision": state.get("decision"), "llm_error": state.get("llm_error"),
                             "clarification": state.get("clarification"),
                             "next_node": state.get("next_node"), "resolution": state.get("resolution")}
        conn.execute("INSERT INTO events(ticket_id,node,payload,created_at) VALUES (?,?,?,?)",
                     (state["ticket"]["id"], node, mask_pii(json.dumps(payload, ensure_ascii=False)), now_iso()))
        if reconcile:
            conn.execute("UPDATE operations SET status='completed',response=? WHERE ticket_id=? AND kind='workflow' AND status='pending'",
                         (json.dumps(state, ensure_ascii=False), state["ticket"]["id"]))
        if resolve_tools:
            conn.execute("UPDATE operations SET status='reconciled',response=? WHERE ticket_id=? AND kind='tool' AND status='pending'",
                         (json.dumps(state.get("resolution"), ensure_ascii=False), state["ticket"]["id"]))

    @contextmanager
    def ticket_lock(self, ticket_id):
        """同工单跨线程/进程互斥，进程退出由操作系统释放；不持有 SQLite 写锁跑模型。"""
        from .errors import OperationPending
        directory = self.db_path.parent / (self.db_path.name + ".locks")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (hashlib.sha256(ticket_id.encode()).hexdigest() + ".lock")
        with path.open("a+b") as handle:
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise OperationPending("工单正在处理中，请稍后重试") from None
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def get_operation(self, key):
        with self._db() as conn:
            row = conn.execute("SELECT status,response,kind FROM operations WHERE key=?", (key,)).fetchone()
        return {"status": row[0], "response": json.loads(row[1]) if row[1] else None, "kind": row[2]} if row else None

    def list_tickets(self):
        with self._db() as conn:
            rows = conn.execute("SELECT payload FROM tickets ORDER BY updated_at DESC, id").fetchall()
        return [json.loads(row[0]) for row in rows]

    def pending_operations(self, ticket_id):
        with self._db() as conn:
            rows = conn.execute("SELECT key,kind,created_at FROM operations WHERE ticket_id=? AND status='pending'", (ticket_id,)).fetchall()
        return [{"key": key, "kind": kind, "created_at": at} for key, kind, at in rows]

    def save_state(self, state):
        with self._db() as conn:
            conn.execute("INSERT OR REPLACE INTO states VALUES (?,?)", (state["ticket"]["id"], json.dumps(state, ensure_ascii=False)))

    def get_state(self, ticket_id):
        with self._db() as conn:
            row = conn.execute("SELECT payload FROM states WHERE id=?", (ticket_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @contextmanager
    def _db(self):
        """给 with 语句提供连接，并确保最后关闭文件句柄。

        用法：with self._db() as conn: ...
        yield 前是进入时做的事，yield 后是正常离开时做的事。
        """
        conn = None
        try:
            conn = self._connect()

            yield conn

            conn.commit()
        except sqlite3.Error:
            raise StorageError() from None
        finally:

            if conn is not None:
                conn.close()

    def save_ticket(self, ticket: Dict[str, Any]) -> None:
        """输入工单字典，按 id 保存；相同 id 再保存时替换旧记录。"""
        with self._db() as conn:

            conn.execute(
                "INSERT OR REPLACE INTO tickets(id, payload, updated_at) VALUES (?, ?, ?)",
                (ticket["id"], json.dumps(ticket, ensure_ascii=False), ticket["updated_at"]),
            )

    def get_ticket(self, ticket_id: str) -> Dict[str, Any] | None:
        """输入工单编号；找到返回字典，找不到返回 None。"""
        with self._db() as conn:

            row = conn.execute("SELECT payload FROM tickets WHERE id = ?", (ticket_id,)).fetchone()

        return json.loads(row[0]) if row else None

    def save_approval(self, approval: Dict[str, Any]) -> None:
        """按审批编号保存字典，同时单独存关联的工单编号。"""
        with self._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO approvals(id, ticket_id, payload) VALUES (?, ?, ?)",
                (approval["id"], approval["ticket_id"], json.dumps(approval, ensure_ascii=False)),
            )

    def get_approval(self, approval_id: str) -> Dict[str, Any] | None:
        """输入审批编号，读取等待状态或已保存的审批决定。"""
        with self._db() as conn:
            row = conn.execute("SELECT payload FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def log_event(self, ticket_id: str, node: str, payload: Dict[str, Any]) -> None:
        """为工单追加一个节点事件；先转文本、再遮盖 PII、最后入库。"""
        safe = mask_pii(json.dumps(payload, ensure_ascii=False))
        with self._db() as conn:

            conn.execute(
                "INSERT INTO events(ticket_id, node, payload, created_at) VALUES (?, ?, ?, ?)",
                (ticket_id, node, safe, now_iso()),
            )

    def list_events(self, ticket_id: str) -> List[Dict[str, Any]]:
        """返回某张工单的所有节点事件；没有事件时返回空列表。"""
        with self._db() as conn:

            rows = conn.execute(
                "SELECT node, payload, created_at FROM events WHERE ticket_id = ? ORDER BY id",
                (ticket_id,),
            ).fetchall()

        return [{"node": n, "payload": json.loads(p), "created_at": t} for n, p, t in rows]
