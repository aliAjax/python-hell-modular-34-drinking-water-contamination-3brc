import json
import sqlite3
from datetime import datetime, timezone

from . import allocation
from .audit import audit_hash, canonical_json
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
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    capacity REAL NOT NULL,
                    unit TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dispatch_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_no TEXT NOT NULL UNIQUE,
                    item_id INTEGER NOT NULL,
                    source_id INTEGER NOT NULL,
                    source_code TEXT NOT NULL,
                    base_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    replayed INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id),
                    FOREIGN KEY(source_id) REFERENCES water_sources(id)
                );
                CREATE TABLE IF NOT EXISTS dispatch_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    source_id INTEGER NOT NULL,
                    zone_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    committed_qty REAL NOT NULL DEFAULT 0,
                    delivered_qty REAL NOT NULL DEFAULT 0,
                    released_qty REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(order_id, zone_id),
                    FOREIGN KEY(order_id) REFERENCES dispatch_orders(id)
                );
                CREATE TABLE IF NOT EXISTS delivery_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_id INTEGER NOT NULL,
                    result TEXT NOT NULL,
                    reported_qty REAL NOT NULL DEFAULT 0,
                    rollback_qty REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(entry_id) REFERENCES dispatch_entries(id)
                );
                CREATE TABLE IF NOT EXISTS allocation_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL,
                    entry_id INTEGER,
                    item_id INTEGER,
                    order_no TEXT,
                    zone_id TEXT,
                    movement TEXT NOT NULL,
                    qty REAL NOT NULL,
                    actor TEXT,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dispatch_orders_item ON dispatch_orders(item_id);
                CREATE INDEX IF NOT EXISTS idx_dispatch_entries_source ON dispatch_entries(source_id);
                CREATE INDEX IF NOT EXISTS idx_dispatch_entries_item ON dispatch_entries(item_id);
                CREATE INDEX IF NOT EXISTS idx_ledger_source ON allocation_ledger(source_id, id);
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

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None, release_holds=False):
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
            released = []
            if release_holds:
                released = self._release_item_holds(conn, item_id, actor, role, new_status)
            conn.execute("COMMIT")
            result = self.get_item(item_id)
            result["_released_holds"] = released
            return result
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

    # ------------------------------------------------------------------
    # 应急水源分配账
    # ------------------------------------------------------------------

    def _log_ledger(self, conn, source_id, movement, qty, entry_id=None, item_id=None,
                    order_no=None, zone_id=None, actor=None, note=""):
        conn.execute(
            "INSERT INTO allocation_ledger(source_id,entry_id,item_id,order_no,zone_id,movement,qty,actor,note,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (source_id, entry_id, item_id, order_no, zone_id, movement, qty, actor, note, now_iso()),
        )

    def _occupied_qty(self, conn, source_id):
        """占用 = 承诺量 - 已回滚/释放量（送达包含在承诺量内，不可撤）。"""
        row = conn.execute(
            "SELECT COALESCE(SUM(committed_qty - released_qty), 0) AS occupied"
            " FROM dispatch_entries WHERE source_id=?",
            (source_id,),
        ).fetchone()
        return float(row["occupied"] or 0.0)

    @staticmethod
    def _refresh_order_status(statuses):
        if allocation.HELD in statuses:
            return "scheduled" if allocation.REJECTED not in statuses else "partial"
        # 没有未执行占用：只要有送达就是执行完成，否则是未排上/已释放的关闭单
        return "closed"

    def _recompute_order(self, conn, order_id):
        rows = conn.execute("SELECT status FROM dispatch_entries WHERE order_id=?", (order_id,)).fetchall()
        statuses = [row["status"] for row in rows]
        conn.execute(
            "UPDATE dispatch_orders SET status=? WHERE id=?",
            (self._refresh_order_status(statuses), order_id),
        )

    def create_water_source(self, code, name, capacity, unit, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO water_sources(code,name,capacity,unit,created_by,created_role,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (code, name, capacity, unit, actor, role, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source_code", "应急水源编号已存在：%s" % code)
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self._log_ledger(conn, source_id, "registered", capacity, actor=actor,
                             note="登记应急水源容量")
            conn.execute("COMMIT")
            return self.get_water_source(code)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_water_source(self, code):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM water_sources WHERE code=?", (code,)).fetchone()
            if row is None:
                raise NotFoundError("water_source_not_found", "应急水源不存在：%s" % code)
            return self._water_source_detail(conn, row)
        finally:
            conn.close()

    def list_water_sources(self):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM water_sources ORDER BY id").fetchall()
            return [self._water_source_detail(conn, row) for row in rows]
        finally:
            conn.close()

    def _water_source_detail(self, conn, row):
        source = dict(row)
        occupied = self._occupied_qty(conn, source["id"])
        capacity = float(source["capacity"])
        source["occupied_qty"] = round(occupied, 6)
        source["available_qty"] = round(capacity - occupied, 6)
        # 占用去向：按事件 + 片区展开仍生效的占用
        source["allocations"] = self._source_allocations(conn, source["id"])
        return source

    def _source_allocations(self, conn, source_id):
        rows = conn.execute(
            "SELECT e.id AS entry_id, e.item_id, e.zone_id, e.status,"
            " e.committed_qty, e.delivered_qty, e.released_qty, o.order_no, o.created_at"
            " FROM dispatch_entries e JOIN dispatch_orders o ON o.id=e.order_id"
            " WHERE e.source_id=? AND e.committed_qty - e.released_qty > 0"
            " ORDER BY e.seq, e.id",
            (source_id,),
        ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["holding_qty"] = round(float(row["committed_qty"]) - float(row["released_qty"]), 6)
            result.append(value)
        return result

    def create_dispatch_order(self, order_no, item_id, source_code, zones, actor, role, expected_version):
        """在一个事务内完成：版本校验、排队占用、写调度单/片区/台账。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 幂等：重复申请沿用首次调度单（先于版本冲突判定）
            existing = conn.execute(
                "SELECT * FROM dispatch_orders WHERE order_no=?", (order_no,)
            ).fetchone()
            if existing is not None:
                conn.execute("COMMIT")
                detail = self.get_dispatch_order(order_no)
                detail["replayed"] = True
                return detail

            item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is None:
                raise DomainError("expected_version_required", "调度申请需要 expected_version", 400)
            if int(expected_version) != int(item["version"]):
                raise ConflictError("version_conflict", "事件已被其他调度修改，请按最新版本重读后提交")
            if item["status"] in allocation.TERMINAL_ITEM_STATUS:
                raise DomainError("event_closed", "事件已结束（%s），不能再占用应急水源" % item["status"], 409)

            source = conn.execute("SELECT * FROM water_sources WHERE code=?", (source_code,)).fetchone()
            if source is None:
                raise NotFoundError("water_source_not_found", "应急水源不存在：%s" % source_code)

            occupied = self._occupied_qty(conn, source["id"])
            available = float(source["capacity"]) - occupied
            plan = allocation.plan_request(available, zones)

            conn.execute(
                "INSERT INTO dispatch_orders(order_no,item_id,source_id,source_code,base_version,status,"
                "replayed,created_by,created_role,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (order_no, item_id, source["id"], source_code, int(item["version"]),
                 "pending", 0, actor, role, now_iso()),
            )
            order_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

            accepted = 0
            rejected = 0
            for seq, (zone, planned_qty, reason) in enumerate(plan, start=1):
                if planned_qty is None:
                    rejected += 1
                    status = allocation.REJECTED
                    committed = 0.0
                    reason_json = canonical_json(reason)
                else:
                    accepted += 1
                    status = allocation.HELD
                    committed = float(planned_qty)
                    reason_json = None
                conn.execute(
                    "INSERT INTO dispatch_entries(order_id,item_id,source_id,zone_id,seq,committed_qty,"
                    "delivered_qty,released_qty,status,reason,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (order_id, item_id, source["id"], zone["zone_id"], seq, committed,
                     0.0, 0.0, status, reason_json, now_iso(), now_iso()),
                )
                if planned_qty is not None:
                    self._log_ledger(
                        conn, source["id"], "hold", committed,
                        item_id=item_id, order_no=order_no, zone_id=zone["zone_id"], actor=actor,
                        note="排队占用",
                    )
                else:
                    self._log_ledger(
                        conn, source["id"], "reject", 0.0,
                        item_id=item_id, order_no=order_no, zone_id=zone["zone_id"], actor=actor,
                        note=reason["message"],
                    )

            if accepted and rejected:
                order_status = "partial"
            elif accepted:
                order_status = "scheduled"
            else:
                order_status = "rejected"
            conn.execute("UPDATE dispatch_orders SET status=? WHERE id=?", (order_status, order_id))

            # 调度单成立即推进事件版本，使并发的另一次调度必须重读
            new_version = int(item["version"]) + 1
            conn.execute(
                "UPDATE items SET version=?,updated_at=? WHERE id=?",
                (new_version, now_iso(), item_id),
            )
            self.append_audit(conn, item_id, "dispatch", actor, role, {
                "order_no": order_no,
                "source_code": source_code,
                "accepted": accepted,
                "rejected": rejected,
            })
            conn.execute("COMMIT")
            detail = self.get_dispatch_order(order_no)
            detail["replayed"] = False
            return detail
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _entries_for_order(self, conn, order_id):
        rows = conn.execute(
            "SELECT * FROM dispatch_entries WHERE order_id=? ORDER BY seq", (order_id,)
        ).fetchall()
        entries = []
        for row in rows:
            value = dict(row)
            if value["reason"]:
                try:
                    value["reason"] = json.loads(value["reason"])
                except ValueError:
                    pass
            value["deliveries"] = [
                dict(record) for record in conn.execute(
                    "SELECT * FROM delivery_records WHERE entry_id=? ORDER BY id", (value["id"],)
                ).fetchall()
            ]
            entries.append(value)
        return entries

    def get_dispatch_order(self, order_no):
        conn = self.connect()
        try:
            order = conn.execute("SELECT * FROM dispatch_orders WHERE order_no=?", (order_no,)).fetchone()
            if order is None:
                raise NotFoundError("dispatch_order_not_found", "调度单不存在：%s" % order_no)
            result = dict(order)
            result["entries"] = self._entries_for_order(conn, order["id"])
            rejected = [
                {"zone_id": entry["zone_id"], "reason": entry["reason"]}
                for entry in result["entries"]
                if entry["status"] == allocation.REJECTED
            ]
            result["unfulfilled"] = rejected
            result["replayed"] = bool(order["replayed"])
            return result
        finally:
            conn.close()

    def list_dispatch_orders(self, item_id=None):
        conn = self.connect()
        try:
            if item_id is None:
                orders = conn.execute("SELECT order_no FROM dispatch_orders ORDER BY id").fetchall()
            else:
                orders = conn.execute(
                    "SELECT order_no FROM dispatch_orders WHERE item_id=? ORDER BY id", (item_id,)
                ).fetchall()
            order_nos = [row["order_no"] for row in orders]
        finally:
            conn.close()
        return [self.get_dispatch_order(order_no) for order_no in order_nos]

    def report_delivery(self, order_no, zone_id, result, delivered_qty, note, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute(
                "SELECT * FROM dispatch_orders WHERE order_no=?", (order_no,)
            ).fetchone()
            if order is None:
                raise NotFoundError("dispatch_order_not_found", "调度单不存在：%s" % order_no)
            entry = conn.execute(
                "SELECT * FROM dispatch_entries WHERE order_id=? AND zone_id=?",
                (order["id"], zone_id),
            ).fetchone()
            if entry is None:
                raise NotFoundError("zone_entry_not_found", "调度单中没有该片区：%s" % zone_id)
            if entry["status"] == allocation.REJECTED:
                raise DomainError("zone_unfulfilled", "该片区容量不足未排上，不能回报送水", 409)

            outcome = allocation.delivery_outcome(dict(entry), result, delivered_qty)

            new_status = outcome["status"]
            add_delivered = outcome["add_delivered"]
            rollback_qty = outcome["rollback_qty"]
            new_delivered = float(entry["delivered_qty"]) + add_delivered
            new_released = float(entry["released_qty"]) + rollback_qty
            conn.execute(
                "UPDATE dispatch_entries SET status=?,delivered_qty=?,released_qty=?,updated_at=? WHERE id=?",
                (new_status, new_delivered, new_released, now_iso(), entry["id"]),
            )
            conn.execute(
                "INSERT INTO delivery_records(entry_id,result,reported_qty,rollback_qty,note,actor,role,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (entry["id"], result, add_delivered, rollback_qty, note, actor, role, now_iso()),
            )
            if add_delivered > 0:
                # 送达不改变占用总量，只把占用转为实际送达，故不冲减容量
                self._log_ledger(
                    conn, entry["source_id"], "deliver", add_delivered,
                    entry_id=entry["id"], item_id=entry["item_id"], order_no=order_no,
                    zone_id=zone_id, actor=actor, note="片区送达（已送达不可撤）",
                )
            if rollback_qty > 0:
                self._log_ledger(
                    conn, entry["source_id"], "rollback", rollback_qty,
                    entry_id=entry["id"], item_id=entry["item_id"], order_no=order_no,
                    zone_id=zone_id, actor=actor,
                    note="送水失败，回退该片区未送达承诺量，已送达 %s 保留" % round(new_delivered, 6),
                )
            self._recompute_order(conn, order["id"])
            self.append_audit(conn, entry["item_id"], "delivery_%s" % result, actor, role, {
                "order_no": order_no,
                "zone_id": zone_id,
                "delivered_qty": add_delivered,
                "rollback_qty": rollback_qty,
                "status": new_status,
            })
            conn.execute("COMMIT")
            return self.get_dispatch_order(order_no)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _release_item_holds(self, conn, item_id, actor, role, new_status):
        """事件进入终态：未执行占用立刻失效并释放，已送达部分不能撤。

        包括完全未送达的 held 片区，以及部分送达片区尚未送达的余量。
        """
        released = []
        rows = conn.execute(
            "SELECT * FROM dispatch_entries WHERE item_id=?"
            " AND status IN (?, ?)",
            (item_id, allocation.HELD, allocation.DELIVERED_PARTIAL),
        ).fetchall()
        touched_orders = set()
        for row in rows:
            remaining = float(row["committed_qty"]) - float(row["released_qty"]) - float(row["delivered_qty"])
            if remaining <= allocation.EPSILON:
                continue
            remaining = round(remaining, 6)
            new_released = float(row["released_qty"]) + remaining
            final_status = (
                allocation.DELIVERED_PARTIAL
                if float(row["delivered_qty"]) > allocation.EPSILON
                else allocation.RELEASED
            )
            conn.execute(
                "UPDATE dispatch_entries SET status=?,released_qty=?,updated_at=? WHERE id=?",
                (final_status, new_released, now_iso(), row["id"]),
            )
            self._log_ledger(
                conn, row["source_id"], "release", remaining,
                entry_id=row["id"], item_id=item_id, zone_id=row["zone_id"], actor=actor,
                note="事件进入 %s，未执行占用失效释放" % new_status,
            )
            released.append({"zone_id": row["zone_id"], "released_qty": remaining,
                             "delivered_qty": float(row["delivered_qty"])})
            touched_orders.add(row["order_id"])
        for order_id in touched_orders:
            self._recompute_order(conn, order_id)
        if released:
            self.append_audit(conn, item_id, "holds_released", actor, role, {
                "new_status": new_status,
                "released": released,
            })
        return released

    def zone_progress(self, item_id=None, zone_id=None):
        conn = self.connect()
        try:
            sql = (
                "SELECT o.order_no, o.source_code, e.zone_id, e.status, e.committed_qty,"
                " e.delivered_qty, e.released_qty, e.reason, e.updated_at"
                " FROM dispatch_entries e JOIN dispatch_orders o ON o.id=e.order_id WHERE 1=1"
            )
            params = []
            if item_id is not None:
                sql += " AND e.item_id=?"
                params.append(item_id)
            if zone_id is not None:
                sql += " AND e.zone_id=?"
                params.append(zone_id)
            sql += " ORDER BY e.item_id, e.zone_id, e.seq"
            rows = conn.execute(sql, params).fetchall()
            progress = []
            for row in rows:
                value = dict(row)
                if value["reason"]:
                    try:
                        value["reason"] = json.loads(value["reason"])
                    except ValueError:
                        pass
                committed = float(row["committed_qty"])
                released = float(row["released_qty"])
                value["holding_qty"] = round(max(committed - released, 0.0), 6)
                progress.append(value)
            return {"progress": progress}
        finally:
            conn.close()

    def allocation_ledger(self, source_code=None, item_id=None):
        conn = self.connect()
        try:
            sql = (
                "SELECT l.*, s.code AS source_code FROM allocation_ledger l"
                " JOIN water_sources s ON s.id=l.source_id WHERE 1=1"
            )
            params = []
            if source_code is not None:
                sql += " AND s.code=?"
                params.append(source_code)
            if item_id is not None:
                sql += " AND l.item_id=?"
                params.append(item_id)
            sql += " ORDER BY l.id"
            rows = conn.execute(sql, params).fetchall()
            return {"ledger": [dict(row) for row in rows]}
        finally:
            conn.close()
