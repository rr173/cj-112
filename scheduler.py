import math
import time
import uuid
from typing import Dict, List, Optional
from collections import deque

from models import (
    WorkOrder,
    WorkOrderCreate,
    WorkOrderPriority,
    WorkOrderStatus,
)
from collision import (
    cranes_config,
    can_crane_reach_point,
    compute_bearing,
)
from arbiter import (
    find_sector_ids_for_crane_at_angles,
    request_token,
    release_token,
)

work_orders: Dict[str, WorkOrder] = {}
crane_queues: Dict[str, deque] = {}
crane_history: Dict[str, List[str]] = {}

PRIORITY_WEIGHT = {
    WorkOrderPriority.URGENT: 3,
    WorkOrderPriority.NORMAL: 2,
    WorkOrderPriority.LOW: 1,
}


def _init_crane_queue(crane_id: str):
    if crane_id not in crane_queues:
        crane_queues[crane_id] = deque()
    if crane_id not in crane_history:
        crane_history[crane_id] = []


def can_crane_cover(crane_id: str, lift_x: float, lift_y: float,
                    drop_x: float, drop_y: float, weight: float) -> bool:
    config = cranes_config.get(crane_id)
    if not config:
        return False
    if weight > config.max_load:
        return False
    if not can_crane_reach_point(crane_id, lift_x, lift_y):
        return False
    if not can_crane_reach_point(crane_id, drop_x, drop_y):
        return False
    return True


def _get_queue_length(crane_id: str) -> int:
    return len(crane_queues.get(crane_id, deque()))


def _distance_to_lift(crane_id: str, lift_x: float, lift_y: float) -> float:
    config = cranes_config.get(crane_id)
    if not config:
        return float("inf")
    dx = lift_x - config.tower_x
    dy = lift_y - config.tower_y
    return math.sqrt(dx * dx + dy * dy)


def _find_best_crane(lift_x: float, lift_y: float,
                     drop_x: float, drop_y: float, weight: float,
                     priority: WorkOrderPriority) -> Optional[str]:
    candidates = []
    for crane_id in cranes_config:
        if can_crane_cover(crane_id, lift_x, lift_y, drop_x, drop_y, weight):
            candidates.append(crane_id)

    if not candidates:
        return None

    priority_weight = PRIORITY_WEIGHT.get(priority, 2)

    def sort_key(cid):
        queue_len = _get_queue_length(cid)
        distance = _distance_to_lift(cid, lift_x, lift_y)
        adjusted_queue_len = queue_len / priority_weight
        return (adjusted_queue_len, distance, cid)

    candidates.sort(key=sort_key)
    return candidates[0]


def _build_failure_reason(lift_x: float, lift_y: float,
                          drop_x: float, drop_y: float, weight: float) -> str:
    reasons = []
    for crane_id, config in cranes_config.items():
        issues = []
        if weight > config.max_load:
            issues.append(f"预估重量{weight}吨超过最大起重量{config.max_load}吨")
        if not can_crane_reach_point(crane_id, lift_x, lift_y):
            issues.append("无法覆盖起吊点")
        if not can_crane_reach_point(crane_id, drop_x, drop_y):
            issues.append("无法覆盖落点")
        if issues:
            reasons.append(f"{crane_id}({config.name}): {'; '.join(issues)}")

    if not cranes_config:
        return "系统中无可用塔吊"
    return "无塔吊能同时覆盖起吊点和落点。各塔吊不可用原因: " + " | ".join(reasons)


def submit_order(create: WorkOrderCreate) -> Dict:
    now = time.time()
    order_id = f"WO-{uuid.uuid4().hex[:8].upper()}"

    order = WorkOrder(
        order_id=order_id,
        lift_x=create.lift_x,
        lift_y=create.lift_y,
        drop_x=create.drop_x,
        drop_y=create.drop_y,
        weight=create.weight,
        priority=create.priority,
        estimated_duration=create.estimated_duration,
        status=WorkOrderStatus.PENDING,
        created_at=now,
        updated_at=now,
    )
    work_orders[order_id] = order

    best_crane = _find_best_crane(
        create.lift_x, create.lift_y,
        create.drop_x, create.drop_y,
        create.weight,
        create.priority,
    )

    if best_crane is None:
        reason = _build_failure_reason(
            create.lift_x, create.lift_y,
            create.drop_x, create.drop_y,
            create.weight,
        )
        order.failure_reason = reason
        return {
            "order": order,
            "assigned": False,
            "message": f"工单创建成功但自动分配失败: {reason}",
        }

    _assign_order_to_crane(order_id, best_crane)
    return {
        "order": work_orders[order_id],
        "assigned": True,
        "message": f"工单已自动分配给塔吊 {best_crane}",
    }


def _assign_order_to_crane(order_id: str, crane_id: str):
    order = work_orders.get(order_id)
    if not order:
        return
    now = time.time()
    order.status = WorkOrderStatus.ASSIGNED
    order.assigned_crane_id = crane_id
    order.assigned_at = now
    order.updated_at = now
    order.failure_reason = None
    _init_crane_queue(crane_id)

    priority_weight = PRIORITY_WEIGHT.get(order.priority, 2)
    queue = crane_queues[crane_id]

    inserted = False
    for i, existing_id in enumerate(list(queue)):
        existing_order = work_orders.get(existing_id)
        if existing_order:
            existing_weight = PRIORITY_WEIGHT.get(existing_order.priority, 2)
            if priority_weight > existing_weight:
                queue.insert(i, order_id)
                inserted = True
                break

    if not inserted:
        queue.append(order_id)


def manually_assign_order(order_id: str, crane_id: str) -> Dict:
    order = work_orders.get(order_id)
    if not order:
        return {"error": f"工单 {order_id} 不存在"}
    if order.status not in (WorkOrderStatus.PENDING, WorkOrderStatus.ASSIGNED):
        return {"error": f"工单当前状态为 {order.status.value}，无法重新分配"}

    if crane_id not in cranes_config:
        return {"error": f"塔吊 {crane_id} 不存在"}

    if not can_crane_cover(crane_id, order.lift_x, order.lift_y,
                           order.drop_x, order.drop_y, order.weight):
        return {"error": f"塔吊 {crane_id} 无法覆盖该工单的起吊点和/或落点，或预估重量超限"}

    if order.status == WorkOrderStatus.ASSIGNED and order.assigned_crane_id:
        old_crane = order.assigned_crane_id
        if old_crane in crane_queues:
            crane_queues[old_crane] = deque(
                oid for oid in crane_queues[old_crane] if oid != order_id
            )

    _assign_order_to_crane(order_id, crane_id)
    return {
        "order": work_orders[order_id],
        "message": f"工单已手动分配给塔吊 {crane_id}",
    }


def cancel_order(order_id: str) -> Dict:
    order = work_orders.get(order_id)
    if not order:
        return {"error": f"工单 {order_id} 不存在"}
    if order.status != WorkOrderStatus.PENDING:
        return {"error": f"只有待分配状态的工单可以取消，当前状态为 {order.status.value}"}

    now = time.time()
    order.status = WorkOrderStatus.CANCELLED
    order.cancelled_at = now
    order.updated_at = now
    return {
        "order": order,
        "message": "工单已取消",
    }


def reassign_order(order_id: str) -> Dict:
    order = work_orders.get(order_id)
    if not order:
        return {"error": f"工单 {order_id} 不存在"}
    if order.status != WorkOrderStatus.ASSIGNED:
        return {"error": f"只有已分配状态的工单可以退回重新调度，当前状态为 {order.status.value}"}

    old_crane = order.assigned_crane_id
    if old_crane and old_crane in crane_queues:
        crane_queues[old_crane] = deque(
            oid for oid in crane_queues[old_crane] if oid != order_id
        )

    now = time.time()
    order.status = WorkOrderStatus.PENDING
    order.assigned_crane_id = None
    order.assigned_at = None
    order.updated_at = now
    return {
        "order": order,
        "message": "工单已退回待分配状态，可重新调度",
    }


def start_order(order_id: str) -> Dict:
    order = work_orders.get(order_id)
    if not order:
        return {"error": f"工单 {order_id} 不存在"}
    if order.status != WorkOrderStatus.ASSIGNED:
        return {"error": f"只有已分配状态的工单可以开始执行，当前状态为 {order.status.value}"}
    if not order.assigned_crane_id:
        return {"error": "工单未分配塔吊"}

    crane_id = order.assigned_crane_id
    config = cranes_config.get(crane_id)
    if not config:
        return {"error": f"分配的塔吊 {crane_id} 不存在"}

    lift_bearing = compute_bearing(config.tower_x, config.tower_y, order.lift_x, order.lift_y)
    drop_bearing = compute_bearing(config.tower_x, config.tower_y, order.drop_x, order.drop_y)

    sector_ids = find_sector_ids_for_crane_at_angles(crane_id, [lift_bearing, drop_bearing])

    token_results = []
    acquired_sectors = []
    failed_tokens = []
    for sec_id in sector_ids:
        try:
            result = request_token(crane_id, sec_id)
            token_results.append(result)
            if result.get("granted", False):
                acquired_sectors.append(sec_id)
            else:
                failed_tokens.append(sec_id)
        except Exception as e:
            failed_tokens.append(sec_id)
            token_results.append({"sector_id": sec_id, "error": str(e), "granted": False})

    if failed_tokens:
        for sec_id in acquired_sectors:
            try:
                release_token(crane_id, sec_id)
            except Exception:
                pass
        return {
            "error": f"无法获取所有必需的扇区令牌，失败扇区: {', '.join(failed_tokens)}",
            "token_results": token_results,
            "failed_sectors": failed_tokens,
        }

    now = time.time()
    order.status = WorkOrderStatus.EXECUTING
    order.started_at = now
    order.updated_at = now
    order.acquired_sectors = acquired_sectors

    if order_id in crane_queues.get(crane_id, deque()):
        crane_queues[crane_id].remove(order_id)

    return {
        "order": order,
        "token_results": token_results,
        "message": f"工单已开始执行，已成功获取 {len(acquired_sectors)} 个扇区令牌",
    }


def complete_order(order_id: str) -> Dict:
    order = work_orders.get(order_id)
    if not order:
        return {"error": f"工单 {order_id} 不存在"}
    if order.status != WorkOrderStatus.EXECUTING:
        return {"error": f"只有执行中状态的工单可以标记完成，当前状态为 {order.status.value}"}

    from arbiter import token_statuses

    crane_id = order.assigned_crane_id
    if not crane_id:
        return {"error": "工单未分配塔吊，无法完成"}

    for sec_id in order.acquired_sectors:
        ts = token_statuses.get(sec_id)
        if ts and ts.holder_crane_id and ts.holder_crane_id != crane_id:
            return {
                "error": f"塔吊 {crane_id} 未持有扇区 {sec_id} 的令牌，当前持有者为 {ts.holder_crane_id}",
                "sector_id": sec_id,
                "current_holder": ts.holder_crane_id,
            }

    release_results = []
    failed_releases = []
    for sec_id in order.acquired_sectors:
        try:
            result = release_token(crane_id, sec_id)
            release_results.append(result)
            if not result.get("released", True):
                failed_releases.append(sec_id)
        except Exception as e:
            failed_releases.append(sec_id)
            release_results.append({"sector_id": sec_id, "error": str(e), "released": False})

    if failed_releases:
        return {
            "error": f"部分令牌释放失败: {', '.join(failed_releases)}",
            "token_release_results": release_results,
            "failed_sectors": failed_releases,
        }

    now = time.time()
    order.status = WorkOrderStatus.COMPLETED
    order.completed_at = now
    order.updated_at = now
    order.acquired_sectors = []

    if crane_id:
        _init_crane_queue(crane_id)
        crane_history[crane_id].append(order_id)

    return {
        "order": order,
        "token_release_results": release_results,
        "message": "工单已完成，相关扇区令牌已释放",
    }


def get_order(order_id: str) -> Optional[WorkOrder]:
    return work_orders.get(order_id)


def get_crane_queue(crane_id: str) -> List[WorkOrder]:
    if crane_id not in crane_queues:
        return []
    return [work_orders[oid] for oid in crane_queues[crane_id] if oid in work_orders]


def get_crane_history_records(crane_id: str) -> List[WorkOrder]:
    if crane_id not in crane_history:
        return []
    return [work_orders[oid] for oid in crane_history[crane_id] if oid in work_orders]


def get_all_orders(status: Optional[WorkOrderStatus] = None) -> List[WorkOrder]:
    orders = list(work_orders.values())
    if status:
        orders = [o for o in orders if o.status == status]
    return orders
