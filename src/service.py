from . import allocation, domain, rules
from .domain import DomainError


WATER_SOURCE_ROLES = {"coordinator", "dispatcher"}
DISPATCH_ROLES = {"dispatcher", "coordinator"}
DELIVERY_ROLES = {"field_operator", "dispatcher"}


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        # 事件进入终态（取消/恢复）时，同一事务内释放未执行占用，已送达不撤
        release_holds = new_status in allocation.TERMINAL_ITEM_STATUS
        result = self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload,
            expected_version, release_holds,
        )
        result["released_holds"] = result.pop("_released_holds", [])
        return self._enrich_item(result)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        return self._enrich_item(item)

    def _enrich_item(self, item):
        item["sources"] = self.repository.list_sources(item["id"])
        item["audit"] = self.repository.audit_trail(item["id"])
        item["assessment"] = rules.assess(item["payload"])
        # 升级视图：旧数据只有一个备用源字段时仍可查看
        item["dispatch_view"] = {
            "legacy_alternate_source_id": item["payload"].get("alternate_source_id"),
            "orders": self.repository.list_dispatch_orders(item["id"]),
        }
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()

    # ------------------------------------------------------------------
    # 应急水源 / 调度分配账
    # ------------------------------------------------------------------

    def register_water_source(self, payload, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in WATER_SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能登记应急水源", 403)
        normalized = domain.normalize_water_source(payload)
        return self.repository.create_water_source(
            normalized["code"], normalized["name"], normalized["capacity"],
            normalized["unit"], actor, role,
        )

    def list_water_sources(self):
        return self.repository.list_water_sources()

    def get_water_source(self, code):
        return self.repository.get_water_source(code)

    def request_dispatch(self, item_id, payload, actor, role, expected_version):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in DISPATCH_ROLES:
            raise DomainError("forbidden", "当前角色不能申请应急水源调度", 403)
        order_no, source_code, zones = domain.normalize_dispatch(payload)
        return self.repository.create_dispatch_order(
            order_no, item_id, source_code, zones, actor, role, expected_version,
        )

    def get_dispatch_order(self, order_no):
        return self.repository.get_dispatch_order(order_no)

    def list_dispatch_orders(self, item_id=None):
        return self.repository.list_dispatch_orders(item_id)

    def report_delivery(self, item_id, order_no, payload, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in DELIVERY_ROLES:
            raise DomainError("forbidden", "当前角色不能回报片区送水", 403)
        zone_id = payload.get("zone_id")
        if not isinstance(zone_id, str) or not zone_id.strip():
            raise DomainError("field_required", "zone_id 不能为空")
        result, delivered_qty, note = domain.normalize_delivery(payload)
        return self.repository.report_delivery(
            order_no, zone_id.strip(), result, delivered_qty, note, actor, role,
        )

    def zone_progress(self, item_id=None, zone_id=None):
        return self.repository.zone_progress(item_id, zone_id)

    def allocation_ledger(self, source_code=None, item_id=None):
        return self.repository.allocation_ledger(source_code, item_id)
