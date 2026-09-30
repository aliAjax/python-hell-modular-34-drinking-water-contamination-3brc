from . import domain, rules
from .domain import DomainError, require_text
from .allocation import (
    ALLOCATION_ROLES,
    DELIVERY_ROLES,
    SOURCE_MANAGE_ROLES,
    normalize_allocation_create,
    normalize_source_create,
)


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
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        # 兼容旧数据：升级前只有一个备用源字段，仍在响应记录中保留可查看
        item["alternate_source_id"] = item["payload"].get("alternate_source_id")
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()

    # ---------------- 应急水源与调度账 ----------------

    def create_water_source(self, payload, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in SOURCE_MANAGE_ROLES:
            raise DomainError("forbidden", "当前角色不能登记应急水源", 403)
        normalized = normalize_source_create(payload)
        return self.repository.create_water_source(
            normalized["external_id"],
            normalized["name"],
            normalized["capacity"],
            normalized["unit"],
            actor,
            role,
        )

    def list_water_sources(self):
        return self.repository.list_water_sources()

    def get_water_source(self, ref):
        return self.repository.get_water_source(ref)

    def create_allocation(self, item_id, payload, actor, role, expected_version=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in ALLOCATION_ROLES:
            raise DomainError("forbidden", "当前角色不能申请应急水源调度", 403)
        if expected_version is None:
            raise DomainError("expected_version_required", "调度申请需要 expected_version", 400)
        normalized = normalize_allocation_create(payload)
        return self.repository.create_allocation(
            item_id,
            normalized["source_id"],
            normalized["request_id"],
            normalized["zones"],
            normalized["note"],
            actor,
            role,
            expected_version,
        )

    def list_allocations(self, item_id):
        return self.repository.list_allocations(item_id)

    def get_allocation(self, allocation_id):
        return self.repository.get_allocation(allocation_id)

    def deliver_zone(self, allocation_id, payload, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in DELIVERY_ROLES:
            raise DomainError("forbidden", "当前角色不能执行送水确认", 403)
        zone_id = require_text(payload, "zone_id")
        success = bool(payload.get("success"))
        reason = payload.get("reason", "")
        if not success and not (isinstance(reason, str) and reason.strip()):
            raise DomainError("reason_required", "送水失败需要填写原因", 400)
        return self.repository.deliver_zone(
            allocation_id, zone_id, success, reason.strip() if isinstance(reason, str) else "", actor, role
        )

    def zone_progress(self, item_id):
        return self.repository.zone_progress(item_id)
