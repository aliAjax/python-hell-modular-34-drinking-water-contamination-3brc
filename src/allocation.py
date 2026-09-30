from .domain import DomainError, require_text, number

# 应急水源与调度角色
SOURCE_MANAGE_ROLES = {"coordinator", "dispatcher"}
ALLOCATION_ROLES = {"coordinator", "dispatcher"}
DELIVERY_ROLES = {"coordinator", "dispatcher", "field_operator"}

# 占用行状态
PENDING = "pending"          # 已占用，待送水
DELIVERED = "delivered"      # 已送达
FAILED = "failed"            # 送水失败，已回滚释放
RELEASED = "released"        # 事件关闭，未执行占用释放
REJECTED = "rejected"        # 容量不足，未排上

# 计入水源余量占用的状态
OCCUPYING_STATUSES = (PENDING, DELIVERED)

INSUFFICIENT_CAPACITY = "insufficient_capacity"
EVENT_CLOSED = "event_closed"

# 事件进入这些终态时，未执行的占用立即失效并释放
TERMINAL_EVENT_STATUSES = {"restored", "cancelled"}


def normalize_source_create(payload):
    external_id = require_text(payload, "external_id")
    name = require_text(payload, "name")
    capacity = number(payload, "capacity", minimum=0)
    if capacity <= 0:
        raise DomainError("invalid_capacity", "水源容量必须大于 0")
    unit = payload.get("unit", "m3")
    if not isinstance(unit, str) or not unit.strip():
        unit = "m3"
    return {
        "external_id": external_id,
        "name": name,
        "capacity": capacity,
        "unit": unit.strip(),
    }


def normalize_allocation_create(payload):
    source_id = require_text(payload, "source_id")
    request_id = require_text(payload, "request_id")
    raw_zones = payload.get("zones", [])
    if not isinstance(raw_zones, list) or not raw_zones:
        raise DomainError("zones_required", "至少需要一个送水片区")
    zones = []
    for raw in raw_zones:
        if not isinstance(raw, dict):
            raise DomainError("invalid_zones", "片区必须是包含 zone_id 和 amount 的对象")
        zone_id = require_text(raw, "zone_id")
        amount = number(raw, "amount", minimum=0)
        if amount <= 0:
            raise DomainError("invalid_amount", "送水量必须大于 0")
        zones.append({"zone_id": zone_id, "amount": amount})
    note = payload.get("note", "")
    if not isinstance(note, str):
        note = ""
    return {
        "source_id": source_id,
        "request_id": request_id,
        "zones": zones,
        "note": note,
    }


def plan_fulfillment(requested, occupied, capacity):
    """按申请先后顺序占用容量。

    容量不够时只落能执行的片区（整片区占用），排不上的片区原样返回原因，
    不占用后续片区的额度。
    """
    fulfilled = []
    rejected = []
    running = occupied
    for req in requested:
        amount = req["amount"]
        if running + amount <= capacity:
            fulfilled.append(dict(req))
            running += amount
        else:
            rejected.append(
                {"zone_id": req["zone_id"], "amount": amount, "reason": INSUFFICIENT_CAPACITY}
            )
    return fulfilled, rejected
