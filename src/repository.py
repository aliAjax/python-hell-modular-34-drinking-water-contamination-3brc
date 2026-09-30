import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .allocation import (
    PENDING,
    DELIVERED,
    FAILED,
    RELEASED,
    REJECTED,
    EVENT_CLOSED,
    TERMINAL_EVENT_STATUSES,
    plan_fulfillment,
)
from .domain import ConflictError, NotFoundError, DomainError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS water_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    capacity REAL NOT NULL,
                    unit TEXT NOT NULL DEFAULT 'm3',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS allocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_id INTEGER NOT NULL,
                    request_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, request_id),
                    FOREIGN KEY(item_id) REFERENCES items(id),
                    FOREIGN KEY(source_id) REFERENCES water_sources(id)
                );
                CREATE TABLE IF NOT EXISTS allocation_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    allocation_id INTEGER NOT NULL,
                    zone_id TEXT NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    delivered_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(allocation_id) REFERENCES allocations(id)
                );
                """
            )
        finally:
            conn.close()

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            conn.execute("COMMIT")
            return {"id": source_id, "item_id": item_id, "source_type": source_type, "external_id": external_id, "payload": payload, "observed_at": observed_at}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            if new_status in TERMINAL_EVENT_STATUSES:
                # 事件关闭：未执行的占用立刻失效并释放额度，已送达的保留不动
                conn.execute(
                    "UPDATE allocation_items SET status=?, reason=?, updated_at=? "
                    "WHERE allocation_id IN (SELECT id FROM allocations WHERE item_id=?) AND status=?",
                    (RELEASED, EVENT_CLOSED, now_iso(), item_id, PENDING),
                )
                self.append_audit(
                    conn,
                    item_id,
                    "unexecuted_allocations_released",
                    actor,
                    role,
                    {"reason": EVENT_CLOSED, "status": new_status},
                )
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()

    # ---------------- 应急水源与调度账 ----------------

    def _occupied_amount(self, conn, source_id):
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM allocation_items "
            "WHERE allocation_id IN (SELECT id FROM allocations WHERE source_id=?) "
            "AND status IN (?, ?)",
            (source_id, PENDING, DELIVERED),
        ).fetchone()
        return float(row["total"])

    def _allocation_items(self, conn, allocation_id):
        rows = conn.execute(
            "SELECT * FROM allocation_items WHERE allocation_id=? ORDER BY id",
            (allocation_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _get_allocation(self, conn, allocation_id):
        row = conn.execute(
            "SELECT a.*, ws.external_id AS source_external_id, ws.name AS source_name, ws.unit AS source_unit "
            "FROM allocations a JOIN water_sources ws ON a.source_id=ws.id WHERE a.id=?",
            (allocation_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("allocation_not_found", "调度单不存在")
        result = dict(row)
        result["items"] = self._allocation_items(conn, allocation_id)
        return result

    def create_water_source(self, external_id, name, capacity, unit, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM water_sources WHERE external_id=?", (external_id,)
            ).fetchone()
            if existing is not None:
                conn.execute("COMMIT")
                return self.get_water_source(external_id)
            conn.execute(
                "INSERT INTO water_sources(external_id,name,capacity,unit,created_at) VALUES(?,?,?,?,?)",
                (external_id, name, float(capacity), unit, now_iso()),
            )
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                None,
                "water_source_created",
                actor,
                role,
                {"external_id": external_id, "name": name, "capacity": float(capacity), "unit": unit},
            )
            conn.execute("COMMIT")
            return self.get_water_source_by_id(source_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_water_source_by_id(self, source_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM water_sources WHERE id=?", (source_id,)).fetchone()
            if row is None:
                raise NotFoundError("water_source_not_found", "应急水源不存在")
            return self._water_source_view(conn, dict(row))
        finally:
            conn.close()

    def get_water_source(self, ref):
        conn = self.connect()
        try:
            try:
                numeric = int(ref)
            except (TypeError, ValueError):
                numeric = -1
            row = conn.execute(
                "SELECT * FROM water_sources WHERE external_id=? OR id=?",
                (str(ref), numeric),
            ).fetchone()
            if row is None:
                raise NotFoundError("water_source_not_found", "应急水源不存在")
            return self._water_source_view(conn, dict(row))
        finally:
            conn.close()

    def _water_source_view(self, conn, source):
        source["occupied"] = self._occupied_amount(conn, source["id"])
        source["available"] = float(source["capacity"]) - source["occupied"]
        source["occupations"] = self._source_occupations(conn, source["id"])
        return source

    def _source_occupations(self, conn, source_id):
        rows = conn.execute(
            "SELECT ai.id, ai.allocation_id, ai.zone_id, ai.amount, ai.status, ai.reason, "
            "a.item_id, a.request_id "
            "FROM allocation_items ai JOIN allocations a ON ai.allocation_id=a.id "
            "WHERE a.source_id=? AND ai.status IN (?, ?) ORDER BY ai.id",
            (source_id, PENDING, DELIVERED),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_water_sources(self):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM water_sources ORDER BY id").fetchall()
            result = []
            for row in rows:
                source = dict(row)
                source["occupied"] = self._occupied_amount(conn, source["id"])
                source["available"] = float(source["capacity"]) - source["occupied"]
                result.append(source)
            return result
        finally:
            conn.close()

    def create_allocation(self, item_id, source_external_id, request_id, zones, note, actor, role, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            item = dict(row)
            if item["status"] in TERMINAL_EVENT_STATUSES:
                raise DomainError("event_closed", "事件已关闭，不能申请应急水源调度", 409)
            # 幂等：重复请求沿用首次调度单，不做版本校验、不重复占用
            existing = conn.execute(
                "SELECT id FROM allocations WHERE item_id=? AND request_id=?",
                (item_id, request_id),
            ).fetchone()
            if existing is not None:
                conn.execute("COMMIT")
                return self._get_allocation(self.connect(), existing["id"])
            if expected_version is not None and int(expected_version) != int(item["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            source = conn.execute(
                "SELECT * FROM water_sources WHERE external_id=?", (source_external_id,)
            ).fetchone()
            if source is None:
                raise NotFoundError("water_source_not_found", "应急水源不存在")
            source = dict(source)
            occupied = self._occupied_amount(conn, source["id"])
            fulfilled, rejected = plan_fulfillment(zones, occupied, float(source["capacity"]))
            now = now_iso()
            conn.execute(
                "INSERT INTO allocations(item_id,source_id,request_id,status,note,created_by,created_role,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (item_id, source["id"], request_id, PENDING, note, actor, role, now, now),
            )
            allocation_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            for req in fulfilled:
                conn.execute(
                    "INSERT INTO allocation_items(allocation_id,zone_id,amount,status,reason,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (allocation_id, req["zone_id"], float(req["amount"]), PENDING, "", now, now),
                )
            for req in rejected:
                conn.execute(
                    "INSERT INTO allocation_items(allocation_id,zone_id,amount,status,reason,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (allocation_id, req["zone_id"], float(req["amount"]), REJECTED, req["reason"], now, now),
                )
            self.append_audit(
                conn,
                item_id,
                "allocation_created",
                actor,
                role,
                {
                    "allocation_id": allocation_id,
                    "source_id": source_external_id,
                    "request_id": request_id,
                    "fulfilled": fulfilled,
                    "rejected": rejected,
                },
            )
            conn.execute(
                "UPDATE items SET version=version+1, updated_at=? WHERE id=?",
                (now, item_id),
            )
            conn.execute("COMMIT")
            return self._get_allocation(self.connect(), allocation_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_allocations(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT a.*, ws.external_id AS source_external_id, ws.name AS source_name, ws.unit AS source_unit "
                "FROM allocations a JOIN water_sources ws ON a.source_id=ws.id "
                "WHERE a.item_id=? ORDER BY a.id DESC",
                (item_id,),
            ).fetchall()
            result = []
            for row in rows:
                alloc = dict(row)
                alloc["items"] = self._allocation_items(conn, alloc["id"])
                result.append(alloc)
            return result
        finally:
            conn.close()

    def get_allocation(self, allocation_id):
        conn = self.connect()
        try:
            return self._get_allocation(conn, allocation_id)
        finally:
            conn.close()

    def deliver_zone(self, allocation_id, zone_id, success, reason, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT ai.*, a.item_id FROM allocation_items ai "
                "JOIN allocations a ON ai.allocation_id=a.id "
                "WHERE ai.allocation_id=? AND ai.zone_id=?",
                (allocation_id, zone_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("allocation_item_not_found", "调度单片区不存在")
            item = dict(row)
            if item["status"] != PENDING:
                raise DomainError(
                    "invalid_state", "当前片区状态 %s 不能执行送水" % item["status"], 409
                )
            now = now_iso()
            if success:
                conn.execute(
                    "UPDATE allocation_items SET status=?, delivered_at=?, reason=?, updated_at=? WHERE id=?",
                    (DELIVERED, now, "", now, item["id"]),
                )
                self.append_audit(
                    conn,
                    item["item_id"],
                    "zone_delivered",
                    actor,
                    role,
                    {"allocation_id": allocation_id, "zone_id": zone_id, "amount": item["amount"]},
                )
            else:
                # 送水失败：只回滚该片区占用，额度立即释放；已送达记录保留
                conn.execute(
                    "UPDATE allocation_items SET status=?, reason=?, updated_at=? WHERE id=?",
                    (FAILED, reason or "delivery_failed", now, item["id"]),
                )
                self.append_audit(
                    conn,
                    item["item_id"],
                    "zone_delivery_failed",
                    actor,
                    role,
                    {
                        "allocation_id": allocation_id,
                        "zone_id": zone_id,
                        "amount": item["amount"],
                        "reason": reason,
                    },
                )
            conn.execute(
                "UPDATE items SET version=version+1, updated_at=? WHERE id=?",
                (now, item["item_id"]),
            )
            conn.execute("COMMIT")
            return self._get_allocation(self.connect(), allocation_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def zone_progress(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT ai.zone_id, "
                "SUM(CASE WHEN ai.status=? THEN ai.amount ELSE 0 END) AS delivered, "
                "SUM(CASE WHEN ai.status=? THEN ai.amount ELSE 0 END) AS pending, "
                "SUM(CASE WHEN ai.status=? THEN ai.amount ELSE 0 END) AS failed, "
                "SUM(CASE WHEN ai.status=? THEN ai.amount ELSE 0 END) AS released, "
                "SUM(CASE WHEN ai.status=? THEN ai.amount ELSE 0 END) AS rejected "
                "FROM allocation_items ai JOIN allocations a ON ai.allocation_id=a.id "
                "WHERE a.item_id=? GROUP BY ai.zone_id",
                (DELIVERED, PENDING, FAILED, RELEASED, REJECTED, item_id),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
