import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class AllocationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.event_payload = {
            "source_id": "SRC-1",
            "contaminant": "nitrate",
            "detected_at": "2026-09-27T06:00:00+00:00",
            "concentration": 20,
            "limit": 10,
            "zone_ids": ["Z-1", "Z-2", "Z-3"],
            "population": 5000,
            "complaints": 4,
        }

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _event(self):
        return self.service.create_item(self.event_payload, "analyst-1", "analyst")

    def _source(self, external_id="WS-1", capacity=100):
        return self.service.create_water_source(
            {"external_id": external_id, "name": "应急水源", "capacity": capacity, "unit": "m3"},
            "coord-1",
            "coordinator",
        )

    def _allocate(self, item, source, zones, request_id="REQ-1"):
        return self.service.create_allocation(
            item["id"],
            {"source_id": source["external_id"], "request_id": request_id, "zones": zones},
            "disp-1",
            "dispatcher",
            item["version"],
        )

    def test_allocation_occupies_capacity_in_queue_order(self):
        source = self._source(capacity=100)
        item = self._event()
        allocation = self._allocate(item, source, [
            {"zone_id": "Z-1", "amount": 60},
            {"zone_id": "Z-2", "amount": 30},
        ])
        statuses = {row["zone_id"]: row["status"] for row in allocation["items"]}
        self.assertEqual(statuses["Z-1"], "pending")
        self.assertEqual(statuses["Z-2"], "pending")
        view = self.service.get_water_source(source["external_id"])
        self.assertEqual(view["occupied"], 90)
        self.assertEqual(view["available"], 10)

    def test_insufficient_capacity_only_lands_executable_zones(self):
        source = self._source(capacity=100)
        item = self._event()
        allocation = self._allocate(item, source, [
            {"zone_id": "Z-1", "amount": 60},
            {"zone_id": "Z-2", "amount": 60},
            {"zone_id": "Z-3", "amount": 30},
        ])
        by_zone = {row["zone_id"]: row for row in allocation["items"]}
        # 按排队：Z-1 落下；Z-2 容量不够排不上；Z-3 较小仍能落下
        self.assertEqual(by_zone["Z-1"]["status"], "pending")
        self.assertEqual(by_zone["Z-2"]["status"], "rejected")
        self.assertEqual(by_zone["Z-2"]["reason"], "insufficient_capacity")
        self.assertEqual(by_zone["Z-3"]["status"], "pending")
        view = self.service.get_water_source(source["external_id"])
        self.assertEqual(view["occupied"], 90)
        self.assertEqual(view["available"], 10)

    def test_delivery_failure_rolls_back_only_that_zone(self):
        source = self._source(capacity=100)
        item = self._event()
        allocation = self._allocate(item, source, [
            {"zone_id": "Z-1", "amount": 40},
            {"zone_id": "Z-2", "amount": 30},
        ])
        self.service.deliver_zone(
            allocation["id"], {"zone_id": "Z-1", "success": True}, "field-1", "field_operator"
        )
        self.service.deliver_zone(
            allocation["id"],
            {"zone_id": "Z-2", "success": False, "reason": "pipe_burst"},
            "field-1",
            "field_operator",
        )
        updated = self.service.get_allocation(allocation["id"])
        by_zone = {row["zone_id"]: row for row in updated["items"]}
        # 已送达保留，失败片区回滚
        self.assertEqual(by_zone["Z-1"]["status"], "delivered")
        self.assertEqual(by_zone["Z-2"]["status"], "failed")
        self.assertEqual(by_zone["Z-2"]["reason"], "pipe_burst")
        view = self.service.get_water_source(source["external_id"])
        self.assertEqual(view["occupied"], 40)
        self.assertEqual(view["available"], 60)
        # 回滚释放的额度可被新调度单占用
        item = self.service.get_item(item["id"])
        realloc = self._allocate(item, source, [{"zone_id": "Z-3", "amount": 30}], "REQ-2")
        self.assertEqual(realloc["items"][0]["status"], "pending")

    def test_event_close_releases_pending_keeps_delivered(self):
        source = self._source(capacity=100)
        item = self._event()
        allocation = self._allocate(item, source, [
            {"zone_id": "Z-1", "amount": 40},
            {"zone_id": "Z-2", "amount": 30},
        ])
        self.service.deliver_zone(
            allocation["id"], {"zone_id": "Z-1", "success": True}, "field-1", "field_operator"
        )
        item = self.service.get_item(item["id"])
        item = self.service.act(
            item["id"], "cancel", {"reason": "污染源已切断"}, "coord-1", "coordinator", item["version"]
        )
        self.assertEqual(item["status"], "cancelled")
        updated = self.service.get_allocation(allocation["id"])
        by_zone = {row["zone_id"]: row for row in updated["items"]}
        self.assertEqual(by_zone["Z-1"]["status"], "delivered")
        self.assertEqual(by_zone["Z-2"]["status"], "released")
        self.assertEqual(by_zone["Z-2"]["reason"], "event_closed")
        view = self.service.get_water_source(source["external_id"])
        self.assertEqual(view["occupied"], 40)

    def test_version_conflict_on_concurrent_allocation(self):
        source = self._source(capacity=100)
        item = self._event()
        first = self.service.create_allocation(
            item["id"],
            {"source_id": source["external_id"], "request_id": "REQ-A",
             "zones": [{"zone_id": "Z-1", "amount": 10}]},
            "disp-1",
            "dispatcher",
            item["version"],
        )
        with self.assertRaises(ConflictError):
            self.service.create_allocation(
                item["id"],
                {"source_id": source["external_id"], "request_id": "REQ-B",
                 "zones": [{"zone_id": "Z-2", "amount": 10}]},
                "disp-2",
                "dispatcher",
                item["version"],
            )
        # 后提交者按版本重读后成功
        item = self.service.get_item(item["id"])
        second = self.service.create_allocation(
            item["id"],
            {"source_id": source["external_id"], "request_id": "REQ-B",
             "zones": [{"zone_id": "Z-2", "amount": 10}]},
            "disp-2",
            "dispatcher",
            item["version"],
        )
        self.assertNotEqual(first["id"], second["id"])

    def test_duplicate_request_uses_first_dispatch_order(self):
        source = self._source(capacity=100)
        item = self._event()
        payload = {
            "source_id": source["external_id"],
            "request_id": "REQ-DUP",
            "zones": [{"zone_id": "Z-1", "amount": 40}],
        }
        first = self.service.create_allocation(
            item["id"], payload, "disp-1", "dispatcher", item["version"]
        )
        # 重复请求（重试）沿用首次调度单，不重复占用
        second = self.service.create_allocation(
            item["id"], payload, "disp-1", "dispatcher", item["version"]
        )
        self.assertEqual(first["id"], second["id"])
        view = self.service.get_water_source(source["external_id"])
        self.assertEqual(view["occupied"], 40)

    def test_legacy_alternate_source_still_viewable(self):
        item = self._event()
        item = self.service.act(
            item["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", item["version"]
        )
        item = self.service.act(
            item["id"], "switch_source", {"alternate_source_id": "ALT-OLD"},
            "coord-1", "coordinator", item["version"],
        )
        self.assertEqual(item["alternate_source_id"], "ALT-OLD")
        fetched = self.service.get_item(item["id"])
        self.assertEqual(fetched["payload"]["alternate_source_id"], "ALT-OLD")
        self.assertEqual(fetched["alternate_source_id"], "ALT-OLD")

    def test_source_margin_occupations_and_zone_progress(self):
        source = self._source(capacity=100)
        item = self._event()
        allocation = self._allocate(item, source, [
            {"zone_id": "Z-1", "amount": 40},
            {"zone_id": "Z-2", "amount": 30},
        ])
        self.service.deliver_zone(
            allocation["id"], {"zone_id": "Z-1", "success": True}, "field-1", "field_operator"
        )
        view = self.service.get_water_source(source["external_id"])
        # Z-1 已送达、Z-2 仍待送水，两者都占用容量
        self.assertEqual(view["occupied"], 70)
        self.assertEqual(view["available"], 30)
        zones = {(occ["zone_id"], occ["status"]) for occ in view["occupations"]}
        self.assertIn(("Z-1", "delivered"), zones)
        self.assertIn(("Z-2", "pending"), zones)
        progress = {row["zone_id"]: row for row in self.service.zone_progress(item["id"])}
        self.assertEqual(progress["Z-1"]["delivered"], 40)
        self.assertEqual(progress["Z-1"]["pending"], 0)
        self.assertEqual(progress["Z-2"]["delivered"], 0)
        self.assertEqual(progress["Z-2"]["pending"], 30)

    def test_cannot_allocate_after_event_closed(self):
        source = self._source(capacity=100)
        item = self._event()
        item = self.service.act(
            item["id"], "cancel", {"reason": "误报"}, "coord-1", "coordinator", item["version"]
        )
        with self.assertRaises(DomainError) as context:
            self._allocate(item, source, [{"zone_id": "Z-1", "amount": 10}])
        self.assertEqual(context.exception.code, "event_closed")


if __name__ == "__main__":
    unittest.main()
