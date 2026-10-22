"""存储层：将 Python 字典转换为 JSON 文本，再保存到 SQLite 文件。

SQLite 是 Python 自带支持的小型数据库，不需要单独启动数据库服务器。
维护 tickets、approvals、events、states 和 operations，分别保存业务、快照和幂等操作。
“持久化”就是数据落到硬盘文件，程序退出后还能读回来。

注意：仅 events.payload 做简单邮箱/手机号遮盖；工单和审批的原始字段未脱敏。
PII 指可识别个人身份的信息；本示例的正则匹配不覆盖全部个人信息。
"""

# json 负责字典与文本互转；re 负责按文本模式查找和替换。
import json
import re
# sqlite3 负责连接数据库并执行 SQL（数据库查询语言）。
import sqlite3
import hashlib
import os
# contextmanager 将带 yield 的函数变成可以配合 with 使用的上下文管理器。
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List

from .models import now_iso
from .errors import StorageError


def mask_pii(value: str) -> str:
    """输入一段文本，返回遮盖常见邮箱和 1 开头的 11 位号码后的新文本。"""
    # re.sub(匹配模式, 替换文本, 原文本)。r 前缀让反斜杠按正则需要保留。
    # \w 包含字母、数字、下划线及 Unicode 词字符；+ 表示前一部分出现一次以上。
    # @ 左右是用户名和域名；\. 匹配真正的点；{2,} 表示至少两个字母。
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "***@***", value)
    # \d 是数字，1\d{10} 是 1 后跟 10 位数字。
    # (?<!\d) / (?!\d) 要求前后不是数字，避免截取更长编号的一部分。
    return re.sub(r"(?<!\d)1\d{10}(?!\d)", "1**********", value)


class AuditStore:
    """保存工单、审批和节点事件，确保程序重启后仍可查询。"""

    def __init__(self, db_path: Path, timeout: float = 5):
        """输入数据库文件路径，增量创建表；表已存在时保留原数据。"""
        self.db_path = Path(db_path)
        self.timeout = timeout
        # parent 是文件的上级目录。parents=True 连上级目录一起创建；
        # exist_ok=True 表示目录已存在也不报错。
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # with 进入 _db() 取得连接，离开时由 _db() 负责提交和关闭。
        with self._db() as conn:
            # executescript 可一次执行多条 SQL；三引号允许写多行字符串。
            # CREATE TABLE IF NOT EXISTS：仅在表不存在时建表。
            # TEXT 是文本列；PRIMARY KEY 是唯一标识；NOT NULL 不允许空值。
            # payload 放整张对象的 JSON；events 的整数主键自动递增。
            # ticket_id 是代码约定的关联字段，此处未定义 SQL 外键约束。
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
        # 每次操作独立连接，避免将同一个 SQLite 连接跨 HTTP 请求线程使用。
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
            # 暂时把连接交给 with 里面的代码；那段代码完成后再从这里继续。
            yield conn
            # 将改动提交到硬盘。如果 with 内发生异常，这行会被跳过。
            conn.commit()
        except sqlite3.Error:
            raise StorageError() from None
        finally:
            # 无论成功还是异常都关闭；未提交的普通事务会随关闭而回滚。
            # sqlite3.Connection 自带的 with 不负责关闭，所以这里显式 close。
            if conn is not None:
                conn.close()

    def save_ticket(self, ticket: Dict[str, Any]) -> None:
        """输入工单字典，按 id 保存；相同 id 再保存时替换旧记录。"""
        with self._db() as conn:
            # SQL 的 ? 是参数占位符；数据用第二个参数传入，避免拼接进 SQL。
            # json.dumps：字典 -> JSON 文本；ensure_ascii=False 保留中文可读性。
            # INSERT OR REPLACE 在主键重复时替换记录，并非完整的版本历史。
            conn.execute(
                "INSERT OR REPLACE INTO tickets(id, payload, updated_at) VALUES (?, ?, ?)",
                (ticket["id"], json.dumps(ticket, ensure_ascii=False), ticket["updated_at"]),
            )

    def get_ticket(self, ticket_id: str) -> Dict[str, Any] | None:
        """输入工单编号；找到返回字典，找不到返回 None。"""
        with self._db() as conn:
            # SELECT 选列，WHERE 限制编号；fetchone() 取第一行。
            # (ticket_id,) 是单元素元组，结尾逗号不能省略。
            row = conn.execute("SELECT payload FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        # row[0] 是结果行的第一列；json.loads：JSON 文本 -> Python 字典。
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
            # 使用 INSERT 追加，不覆盖之前的事件。now_iso() 记录写入时间。
            conn.execute(
                "INSERT INTO events(ticket_id, node, payload, created_at) VALUES (?, ?, ?, ?)",
                (ticket_id, node, safe, now_iso()),
            )

    def list_events(self, ticket_id: str) -> List[Dict[str, Any]]:
        """返回某张工单的所有节点事件；没有事件时返回空列表。"""
        with self._db() as conn:
            # ORDER BY id 按新增顺序排序，fetchall() 取所有匹配行。
            rows = conn.execute(
                "SELECT node, payload, created_at FROM events WHERE ticket_id = ? ORDER BY id",
                (ticket_id,),
            ).fetchall()
        # 列表推导式：逐行解包成 n（节点）、p（JSON）、t（时间），转为字典。
        # 当前 CLI/API 未暴露这个查询方法，可在 Python 中直接调用它。
        return [{"node": n, "payload": json.loads(p), "created_at": t} for n, p, t in rows]
