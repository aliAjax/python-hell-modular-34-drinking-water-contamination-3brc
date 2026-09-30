from datetime import datetime


class DomainError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class ConflictError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 409)


class NotFoundError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 404)


def require_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def number(payload, name, minimum=None):
    value = payload.get(name)
    if isinstance(value, bool):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    if minimum is not None and value < minimum:
        raise DomainError("invalid_number", "%s 不能小于 %s" % (name, minimum))
    return value


def parse_timestamp(payload, name):
    value = require_text(payload, name)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("invalid_timestamp", "%s 必须是 ISO 时间" % name)
    return value


def normalize_create(payload):
    source_id = require_text(payload, "source_id")
    contaminant = require_text(payload, "contaminant")
    detected_at = parse_timestamp(payload, "detected_at")
    concentration = number(payload, "concentration", 0)
    limit = number(payload, "limit", 0.000001)
    zones = payload.get("zone_ids", [])
    if not isinstance(zones, list) or not zones:
        raise DomainError("zones_required", "至少需要一个受影响区域")
    if any(not isinstance(zone, str) or not zone.strip() for zone in zones):
        raise DomainError("invalid_zones", "区域编号必须是字符串列表")
    population = int(payload.get("population", 0) or 0)
    if population < 0:
        raise DomainError("invalid_population", "受影响人数不能为负数")
    stable_key = "%s|%s|%s" % (source_id, contaminant, detected_at)
    return {
        "source_id": source_id,
        "contaminant": contaminant,
        "detected_at": detected_at,
        "concentration": concentration,
        "limit": limit,
        "zone_ids": [zone.strip() for zone in zones],
        "population": population,
        "complaints": int(payload.get("complaints", 0) or 0),
        "notifications": [],
        "response_actions": [],
        "sample_results": [],
        "_stable_key": stable_key,
    }


def normalize_source(payload):
    source_type = require_text(payload, "source_type")
    external_id = require_text(payload, "external_id")
    observed_at = parse_timestamp(payload, "observed_at")
    result = {
        "source_type": source_type,
        "external_id": external_id,
        "observed_at": observed_at,
        "concentration": number(payload, "concentration", 0) if "concentration" in payload else None,
        "zone_id": payload.get("zone_id"),
        "note": payload.get("note", ""),
    }
    return result


# ---------------------------------------------------------------------------
# 应急水源 / 调度单 / 片区送水
# ---------------------------------------------------------------------------

def normalize_water_source(payload):
    code = require_text(payload, "code")
    name = require_text(payload, "name")
    capacity = number(payload, "capacity", 0.000001)
    unit = payload.get("unit")
    if not isinstance(unit, str) or not unit.strip():
        unit = "tonne"
    return {"code": code, "name": name, "capacity": capacity, "unit": unit.strip()}


def normalize_dispatch(payload):
    """调度申请：一个调度单包含若干片区及承诺量，片区即排队与回滚的最小单位。"""
    order_no = require_text(payload, "order_no")
    raw_zones = payload.get("zones")
    if not isinstance(raw_zones, list) or not raw_zones:
        raise DomainError("zones_required", "至少需要一个送水片区")
    zones = []
    seen = set()
    for zone in raw_zones:
        if not isinstance(zone, dict):
            raise DomainError("invalid_zones", "片区必须是对象列表")
        zone_id = require_text(zone, "zone_id")
        if zone_id in seen:
            raise DomainError("duplicate_zone", "同一调度单内片区不能重复：%s" % zone_id)
        seen.add(zone_id)
        quantity = number(zone, "quantity", 0.000001)
        zones.append({"zone_id": zone_id, "quantity": quantity})
    source_code = require_text(payload, "source_code")
    return order_no, source_code, zones


def normalize_delivery(payload):
    result = payload.get("result")
    if result not in ("delivered", "failed"):
        raise DomainError("invalid_delivery_result", "result 必须是 delivered 或 failed")
    delivered_qty = None
    if "delivered_qty" in payload and payload.get("delivered_qty") is not None:
        delivered_qty = number(payload, "delivered_qty", 0)
    note = payload.get("note", "")
    if not isinstance(note, str):
        note = ""
    return result, delivered_qty, note.strip()
