"""应急水源分配账规则。

核心约定：
- 占用 = 已占用(held) + 已送达(delivered)，送达会把占用转成不可撤的实际量。
- 申请按先后排队（水源 seq 顺序）；水源余量不够时，只落能够整块执行的片区，
  落不上的片区返回未排上的原因，不做跨片区拆分。
- 片区送水失败按回滚只退回该片区尚未送达的承诺量，已送达记录永久保留。
- 事件进入终态后，未执行占用立即失效释放；已送达部分不能撤。
"""
from .domain import DomainError

# 调度片区状态
HELD = "held"                    # 已占用，等待送水
DELIVERED_FULL = "delivered"     # 全部送达
DELIVERED_PARTIAL = "partial"    # 部分送达（剩余承诺量已回滚）
RELEASED = "released"            # 未执行占用被释放（事件终态）
ROLLED_BACK = "rolled_back"      # 送水失败，未送达承诺量已回滚
REJECTED = "rejected"            # 容量不足，排队未排上（未占用任何容量）

ACTIVE_OCCUPYING_STATES = (HELD, DELIVERED_FULL, DELIVERED_PARTIAL)
PENDING_STATES = (HELD,)         # 仍可能被释放或回滚

# 事件终态：终态之后未执行占用立刻失效
TERMINAL_ITEM_STATUS = ("cancelled", "restored")

EPSILON = 1e-9


def plan_request(available, zones):
    """根据当前余量对单次申请的片区做排队规划。

    返回 [(zone, planned_qty or None, reason or None)]；片区按列表顺序逐个尝试，
    余量足够则整块占用，不够则跳过；同一申请中前面片区的占用会挤占后面片区的余量。
    """
    remaining = available
    planned = []
    for zone in zones:
        if zone["quantity"] <= remaining + EPSILON:
            planned.append((zone, zone["quantity"], None))
            remaining -= zone["quantity"]
        else:
            planned.append((
                zone,
                None,
                {
                    "code": "capacity_exhausted",
                    "message": "水源余量不足，片区排队未排上",
                    "requested": zone["quantity"],
                    "available": round(max(remaining, 0.0), 6),
                },
            ))
    return planned


def delivery_outcome(entry, result, delivered_qty):
    """计算片区送水结果，保证不超量、失败只回滚该片区未送达部分。"""
    committed = float(entry["committed_qty"])
    delivered = float(entry["delivered_qty"] or 0)
    released = float(entry.get("released_qty", 0) or 0)
    # 未结承诺量：已回滚/已释放的部分不能再送水，也不会被重复回滚
    remaining = max(committed - delivered - released, 0.0)
    if remaining <= EPSILON:
        raise DomainError("entry_closed", "该片区已关闭（未结承诺量为零），不能再回报送水", 409)

    if result == "delivered":
        if delivered_qty is None:
            amount = remaining
        else:
            amount = float(delivered_qty)
        if amount < -EPSILON or amount > remaining + EPSILON:
            raise DomainError(
                "invalid_delivery_qty",
                "送达量必须在 0 到剩余承诺量 %s 之间" % round(remaining, 6),
            )
        amount = min(max(amount, 0.0), remaining)
        new_delivered = delivered + amount
        if new_delivered + EPSILON >= committed:
            new_status = DELIVERED_FULL
        else:
            new_status = DELIVERED_PARTIAL
        return {
            "status": new_status,
            "add_delivered": amount,
            "rollback_qty": 0.0,
        }

    # result == "failed"：只回滚该片区尚未送达的承诺量，已送达量保留
    return {
        "status": ROLLED_BACK if delivered <= EPSILON else DELIVERED_PARTIAL,
        "add_delivered": 0.0,
        "rollback_qty": remaining,
    }
