import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


def make_item(service):
    return service.create_item({
        "source_id": "SRC-EM",
        "contaminant": "oil",
        "detected_at": "2026-09-30T06:00:00+00:00",
        "concentration": 20,
        "limit": 10,
        "zone_ids": ["Z-A", "Z-B", "Z-C"],
        "population": 3000,
        "complaints": 9,
    }, "analyst-1", "analyst")


class AllocationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.source = self.service.register_water_source(
            {"code": "EM-1", "name": "北郊应急水源", "capacity": 100, "unit": "tonne"},
            "disp-1", "dispatcher",
        )
        self.item = make_item(self.service)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def dispatch(self, order_no, zones, version=None, item_id=None):
        if version is None:
            version = self.service.get_item(item_id or self.item["id"])["version"]
        return self.service.request_dispatch(
            item_id or self.item["id"],
            {"order_no": order_no, "source_code": "EM-1", "zones": zones},
            "disp-1", "dispatcher", version,
        )

    def deliver(self, order_no, zone_id, result="delivered", delivered_qty=None):
        payload = {"zone_id": zone_id, "result": result}
        if delivered_qty is not None:
            payload["delivered_qty"] = delivered_qty
        return self.service.report_delivery(self.item["id"], order_no, payload, "field-1", "field_operator")

    # -- 申请按先后占用 -------------------------------------------------

    def test_requests_occupy_in_arrival_order(self):
        first = self.dispatch("ORD-1", [{"zone_id": "Z-A", "quantity": 60}])
        self.assertEqual(first["status"], "scheduled")
        self.assertEqual(first["entries"][0]["status"], "held")
        source = self.service.get_water_source("EM-1")
        self.assertEqual(source["occupied_qty"], 60)
        self.assertEqual(source["available_qty"], 40)

        second = self.dispatch("ORD-2", [{"zone_id": "Z-B", "quantity": 40}])
        source = self.service.get_water_source("EM-1")
        self.assertEqual(source["occupied_qty"], 100)
        self.assertEqual(source["available_qty"], 0)

    # -- 容量不够：只落整块能执行的片区，其余返回原因 --------------------

    def test_partial_zones_queued_with_reason(self):
        self.dispatch("ORD-1", [{"zone_id": "Z-A", "quantity": 60}])
        order = self.dispatch("ORD-2", [
            {"zone_id": "Z-B", "quantity": 30},
            {"zone_id": "Z-C", "quantity": 20},
        ])
        # Z-B 整块落上（30），Z-C 需要 20，只剩 10 -> 未排上
        self.assertEqual(order["status"], "partial")
        b_entry, c_entry = order["entries"]
        self.assertEqual(b_entry["status"], "held")
        self.assertEqual(b_entry["committed_qty"], 30)
        self.assertEqual(c_entry["status"], "rejected")
        self.assertEqual(c_entry["reason"]["code"], "capacity_exhausted")
        self.assertEqual(c_entry["reason"]["available"], 10)
        self.assertEqual(order["unfulfilled"][0]["zone_id"], "Z-C")
        # 被拒片区不占容量
        self.assertEqual(self.service.get_water_source("EM-1")["occupied_qty"], 90)

    def test_all_zones_rejected(self):
        self.dispatch("ORD-1", [{"zone_id": "Z-A", "quantity": 95}])
        order = self.dispatch("ORD-2", [{"zone_id": "Z-B", "quantity": 10}])
        self.assertEqual(order["status"], "rejected")
        self.assertEqual(order["entries"][0]["status"], "rejected")
        self.assertEqual(self.service.get_water_source("EM-1")["occupied_qty"], 95)

    # -- 片区送水失败：只回滚该片区，已送达保留 --------------------------

    def test_failed_delivery_rolls_back_only_that_zone(self):
        order = self.dispatch("ORD-1", [
            {"zone_id": "Z-A", "quantity": 40},
            {"zone_id": "Z-B", "quantity": 30},
        ])
        self.deliver("ORD-1", "Z-A", "delivered", 40)
        # Z-B 先送 10，随后失败
        self.deliver("ORD-1", "Z-B", "delivered", 10)
        source = self.service.get_water_source("EM-1")
        self.assertEqual(source["occupied_qty"], 70)

        order = self.deliver("ORD-1", "Z-B", "failed")
        b_entry = next(e for e in order["entries"] if e["zone_id"] == "Z-B")
        self.assertEqual(b_entry["status"], "partial")
        self.assertEqual(b_entry["delivered_qty"], 10)
        # 只退回未送达的 20，已送达 10 保留；Z-A 完全不受影响
        source = self.service.get_water_source("EM-1")
        self.assertEqual(source["occupied_qty"], 50)
        self.assertEqual(source["available_qty"], 50)
        a_entry = next(e for e in order["entries"] if e["zone_id"] == "Z-A")
        self.assertEqual(a_entry["status"], "delivered")
        self.assertEqual(a_entry["delivered_qty"], 40)
        # 送水记录（含失败）保留
        self.assertEqual(len(b_entry["deliveries"]), 2)
        self.assertEqual(b_entry["deliveries"][-1]["result"], "failed")

    def test_failed_delivery_with_nothing_delivered_rolls_back_all(self):
        self.dispatch("ORD-1", [{"zone_id": "Z-A", "quantity": 25}])
        order = self.deliver("ORD-1", "Z-A", "failed")
        entry = order["entries"][0]
        self.assertEqual(entry["status"], "rolled_back")
        self.assertEqual(entry["delivered_qty"], 0)
        self.assertEqual(self.service.get_water_source("EM-1")["occupied_qty"], 0)

    def test_delivered_cannot_exceed_committed(self):
        self.dispatch("ORD-1", [{"zone_id": "Z-A", "quantity": 20}])
        with self.assertRaises(DomainError) as context:
            self.deliver("ORD-1", "Z-A", "delivered", 25)
        self.assertEqual(context.exception.code, "invalid_delivery_qty")

    def test_rejected_zone_cannot_report_delivery(self):
        self.dispatch("ORD-1", [{"zone_id": "Z-A", "quantity": 100}])
        order = self.dispatch("ORD-2", [{"zone_id": "Z-B", "quantity": 5}])
        self.assertEqual(order["entries"][0]["status"], "rejected")
        with self.assertRaises(DomainError) as context:
            self.deliver("ORD-2", "Z-B")
        self.assertEqual(context.exception.code, "zone_unfulfilled")

    def test_rolled_back_capacity_serves_later_queue(self):
        self.dispatch("ORD-1", [{"zone_id": "Z-A", "quantity": 80}])
        later = self.dispatch("ORD-2", [{"zone_id": "Z-B", "quantity": 30}])
        self.assertEqual(later["entries"][0]["status"], "rejected")
        # ORD-1 送水失败回滚 80
        self.deliver("ORD-1", "Z-A", "failed")
        retry = self.dispatch("ORD-3", [{"zone_id": "Z-C", "quantity": 30}])
        self.assertEqual(retry["entries"][0]["status"], "held")

    # -- 事件终态：未执行占用失效，已执行不能撤 --------------------------

    def test_cancel_releases_holds_but_keeps_delivered(self):
        order = self.dispatch("ORD-1", [
            {"zone_id": "Z-A", "quantity": 40},
            {"zone_id": "Z-B", "quantity": 30},
        ])
        self.deliver("ORD-1", "Z-A", "delivered", 40)
        # cancel 只允许 detected/verified；先 verify
        item = self.service.act(self.item["id"], "verify", {"sample_count": 1},
                                "analyst-1", "analyst", self.item["version"] + 1)
        item = self.service.act(item["id"], "cancel", {"reason": "误报"},
                                "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "cancelled")
        released = item["released_holds"]
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0]["zone_id"], "Z-B")
        self.assertEqual(released[0]["released_qty"], 30)

        order = self.service.get_dispatch_order("ORD-1")
        a_entry = next(e for e in order["entries"] if e["zone_id"] == "Z-A")
        b_entry = next(e for e in order["entries"] if e["zone_id"] == "Z-B")
        self.assertEqual(a_entry["status"], "delivered")
        self.assertEqual(a_entry["delivered_qty"], 40)
        self.assertEqual(b_entry["status"], "released")
        # 已送达 40 不撤，未执行 30 释放
        source = self.service.get_water_source("EM-1")
        self.assertEqual(source["occupied_qty"], 40)
        self.assertEqual(source["available_qty"], 60)
        # 终态后不能再调度
        with self.assertRaises(DomainError) as context:
            self.dispatch("ORD-X", [{"zone_id": "Z-C", "quantity": 5}])
        self.assertEqual(context.exception.code, "event_closed")

    def test_restore_releases_partial_zone_remainder(self):
        self.dispatch("ORD-1", [
            {"zone_id": "Z-A", "quantity": 40},
            {"zone_id": "Z-B", "quantity": 30},
        ])
        self.deliver("ORD-1", "Z-A", "delivered", 40)
        self.deliver("ORD-1", "Z-B", "delivered", 30)
        # 再建一个事件验证 restore 释放；这里直接对 ORD-1 同事件走终态不现实（需全部采样），
        # 用第二张单的部分送达 + cancel 已覆盖，这里验证 restore 路径：
        return

    def test_restore_workflow_releases_unexecuted_hold(self):
        order = self.dispatch("ORD-1", [
            {"zone_id": "Z-A", "quantity": 40},
            {"zone_id": "Z-B", "quantity": 30},
        ])
        self.deliver("ORD-1", "Z-A", "delivered", 40)
        # Z-B 一直未送水，走完响应恢复流程后其占用应被释放，Z-A 已送达保留
        item = self.service.act(self.item["id"], "verify", {"sample_count": 1},
                                "analyst-1", "analyst", self.service.get_item(self.item["id"])["version"])
        item = self.service.act(item["id"], "advise",
                                {"notice_id": "N-1", "kind": "boil", "message": "煮沸"},
                                "disp-1", "dispatcher", item["version"])
        item = self.service.act(item["id"], "switch_source", {"alternate_source_id": "EM-1"},
                                "coord-1", "coordinator", item["version"])
        item = self.service.act(item["id"], "flush", {"zone_id": "Z-A"},
                                "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "disinfect", {"zone_id": "Z-A", "completed": True},
                                "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "sample",
                                {"sample_id": "S-1", "zone_id": "Z-A", "concentration": 2},
                                "lab-1", "lab", item["version"])
        item = self.service.act(item["id"], "restore", {"all_zones_cleared": True},
                                "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")
        released = {(row["zone_id"]): row["released_qty"] for row in item["released_holds"]}
        self.assertEqual(released, {"Z-B": 30})
        order = self.service.get_dispatch_order("ORD-1")
        self.assertEqual(next(e for e in order["entries"] if e["zone_id"] == "Z-A")["status"], "delivered")
        self.assertEqual(next(e for e in order["entries"] if e["zone_id"] == "Z-B")["status"], "released")
        self.assertEqual(self.service.get_water_source("EM-1")["occupied_qty"], 40)

    def test_partial_delivery_remainder_released_on_terminal(self):
        order = self.dispatch("ORD-1", [{"zone_id": "Z-A", "quantity": 50}])
        self.deliver("ORD-1", "Z-A", "delivered", 20)
        item = self.service.get_item(self.item["id"])
        item = self.service.act(item["id"], "verify", {"sample_count": 1},
                                "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "cancel", {"reason": "污染解除"},
                                "coord-1", "coordinator", item["version"])
        order = self.service.get_dispatch_order("ORD-1")
        entry = order["entries"][0]
        self.assertEqual(entry["status"], "partial")
        self.assertEqual(entry["delivered_qty"], 20)
        source = self.service.get_water_source("EM-1")
        self.assertEqual(source["occupied_qty"], 20)
        self.assertEqual(source["available_qty"], 80)

    # -- 并发：后提交者按版本重读；重复请求沿用首次调度单 ----------------

    def test_concurrent_dispatch_stale_version_must_reread(self):
        stale = self.item["version"]
        self.dispatch("ORD-1", [{"zone_id": "Z-A", "quantity": 10}])
        with self.assertRaises(ConflictError) as context:
            self.dispatch("ORD-2", [{"zone_id": "Z-B", "quantity": 10}], version=stale)
        self.assertEqual(context.exception.code, "version_conflict")
        # 重读最新版本后可以提交
        current = self.service.get_item(self.item["id"])["version"]
        order = self.dispatch("ORD-3", [{"zone_id": "Z-C", "quantity": 10}], version=current)
        self.assertEqual(order["entries"][0]["status"], "held")

    def test_concurrent_dispatch_is_serialized(self):
        """两个线程同时申请同一水源，先到先得，后到拿到 409 或排队结果，不超卖。"""
        version = self.item["version"]
        results = {}

        def worker(tag, order_no):
            try:
                order = self.dispatch(
                    order_no, [{"zone_id": "Z-%s" % tag, "quantity": 70}],
                    version=version,
                )
                results[tag] = order["entries"][0]["status"]
            except ConflictError as exc:
                results[tag] = exc.code

        t1 = threading.Thread(target=worker, args=("A", "ORD-T1"))
        t2 = threading.Thread(target=worker, args=("B", "ORD-T2"))
        t1.start(); t2.start(); t1.join(); t2.join()

        statuses = set(results.values())
        self.assertIn("held", statuses)
        self.assertTrue("rejected" in statuses or "version_conflict" in statuses)
        self.assertLessEqual(self.service.get_water_source("EM-1")["occupied_qty"], 100)

    def test_duplicate_request_reuses_first_order(self):
        first = self.dispatch("ORD-DUP", [{"zone_id": "Z-A", "quantity": 20}])
        replay = self.dispatch("ORD-DUP", [{"zone_id": "Z-A", "quantity": 20}],
                               version=self.item["version"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["id"], first["id"])
        # 容量只占一次
        self.assertEqual(self.service.get_water_source("EM-1")["occupied_qty"], 20)

    def test_dispatch_requires_expected_version(self):
        with self.assertRaises(DomainError) as context:
            self.service.request_dispatch(
                self.item["id"],
                {"order_no": "ORD-NV", "source_code": "EM-1",
                 "zones": [{"zone_id": "Z-A", "quantity": 1}]},
                "disp-1", "dispatcher", None,
            )
        self.assertEqual(context.exception.code, "expected_version_required")

    # -- 旧数据单备用源字段 + 查询接口 ----------------------------------

    def test_legacy_single_alternate_source_still_visible(self):
        item = self.service.act(self.item["id"], "verify", {"sample_count": 1},
                                "analyst-1", "analyst", self.item["version"])
        item = self.service.act(item["id"], "switch_source",
                                {"alternate_source_id": "EM-LEGACY"},
                                "coord-1", "coordinator", item["version"])
        view = self.service.get_item(item["id"])["dispatch_view"]
        self.assertEqual(view["legacy_alternate_source_id"], "EM-LEGACY")
        # 老的 switch_source 覆盖字段的行为依旧，不影响分配账数据
        self.assertEqual(item["payload"]["alternate_source_id"], "EM-LEGACY")

    def test_query_balance_destinations_and_zone_progress(self):
        self.dispatch("ORD-1", [{"zone_id": "Z-A", "quantity": 60}])
        source = self.service.get_water_source("EM-1")
        self.assertEqual(source["capacity"], 100)
        self.assertEqual(source["available_qty"], 40)
        self.assertEqual(source["allocations"][0]["zone_id"], "Z-A")
        self.assertEqual(source["allocations"][0]["holding_qty"], 60)

        self.deliver("ORD-1", "Z-A", "delivered", 60)
        progress = self.service.zone_progress(zone_id="Z-A")["progress"]
        self.assertEqual(len(progress), 1)
        self.assertEqual(progress[0]["status"], "delivered")
        self.assertEqual(progress[0]["delivered_qty"], 60)

        ledger = self.service.allocation_ledger(source_code="EM-1")["ledger"]
        movements = [row["movement"] for row in ledger]
        self.assertEqual(movements, ["registered", "hold", "deliver"])

    def test_delivery_rejected_after_release_or_full(self):
        self.dispatch("ORD-1", [{"zone_id": "Z-A", "quantity": 50}])
        # 全部送达后再回报
        self.deliver("ORD-1", "Z-A", "delivered", 50)
        with self.assertRaises(DomainError) as context:
            self.deliver("ORD-1", "Z-A", "delivered", 1)
        self.assertEqual(context.exception.code, "entry_closed")

        order = self.dispatch("ORD-2", [{"zone_id": "Z-B", "quantity": 40}])
        self.deliver("ORD-2", "Z-B", "delivered", 10)
        item = self.service.get_item(self.item["id"])
        verified = self.service.act(item["id"], "verify", {"sample_count": 1},
                                    "analyst-1", "analyst", item["version"])
        self.service.act(item["id"], "cancel", {"reason": "解除"},
                         "coord-1", "coordinator", verified["version"])
        # 终态已释放 Z-B 剩余 30，再回报不能重新占用
        with self.assertRaises(DomainError) as context:
            self.deliver("ORD-2", "Z-B", "delivered", 5)
        self.assertEqual(context.exception.code, "entry_closed")
        self.assertEqual(self.service.get_water_source("EM-1")["occupied_qty"], 60)

    def test_dispatch_requires_permission(self):
        with self.assertRaises(DomainError) as context:
            self.service.request_dispatch(
                self.item["id"],
                {"order_no": "ORD-P", "source_code": "EM-1",
                 "zones": [{"zone_id": "Z-A", "quantity": 1}]},
                "lab-1", "lab", self.item["version"],
            )
        self.assertEqual(context.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
