import math
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from models import (
    OrderProgressTracking,
    OrderProgressUpdateRecord,
    OrderProgressStagnationAlarm,
    OrderProgressResponse,
    OrderDeviationRatioStats,
    OrderProgressConfig,
    CraneStatus,
    WorkOrderStatus,
    AlarmType,
)
from collision import cranes_config, compute_bearing, normalize_angle
from path_planner import active_path_plans, PathDirection


_progress_config = OrderProgressConfig()

active_progress_tracking: Dict[str, OrderProgressTracking] = {}
completed_progress_history: Dict[str, OrderProgressTracking] = {}
stagnation_alarms: List[OrderProgressStagnationAlarm] = []
crane_current_executing_order: Dict[str, str] = {}


def _angular_distance_cw(start_angle: float, end_angle: float) -> float:
    return (end_angle - start_angle) % 360.0


def _angular_distance_ccw(start_angle: float, end_angle: float) -> float:
    return (start_angle - end_angle) % 360.0


def init_order_progress_module():
    active_progress_tracking.clear()
    completed_progress_history.clear()
    stagnation_alarms.clear()
    crane_current_executing_order.clear()


def update_progress_config(config: OrderProgressConfig):
    global _progress_config
    _progress_config = config


def get_progress_config() -> OrderProgressConfig:
    return _progress_config


def init_order_progress(order_id: str, crane_id: str, has_path_plan: bool):
    now = time.time()
    tracking = OrderProgressTracking(
        order_id=order_id,
        crane_id=crane_id,
        current_progress=0.0,
        estimated_remaining_seconds=None,
        is_stagnated=False,
        stagnation_started_at=None,
        last_progress_update_at=now,
        last_progress_value=0.0,
        progress_history=[],
        stagnation_alarm_generated=False,
        has_path_plan=has_path_plan,
    )
    active_progress_tracking[order_id] = tracking
    crane_current_executing_order[crane_id] = order_id

    from scheduler import work_orders
    order = work_orders.get(order_id)
    if order:
        order.progress_percent = 0.0
        order.last_progress_updated_at = now


def _calculate_progress_by_path(order_id: str, current_angle: float,
                                crane_id: str, trolley_position: float) -> float:
    plan = active_path_plans.get(order_id)
    if not plan:
        return 0.0

    total_sweep = plan.angular_distance
    if total_sweep <= 0:
        return _calculate_progress_by_distance(order_id, crane_id, trolley_position)

    if plan.direction == PathDirection.CW:
        traveled = _angular_distance_cw(plan.lift_angle, current_angle)
    else:
        traveled = _angular_distance_ccw(plan.lift_angle, current_angle)

    traveled = min(traveled, total_sweep)
    progress = (traveled / total_sweep) * 100.0
    return min(max(progress, 0.0), 100.0)


def _calculate_progress_by_distance(order_id: str, crane_id: str, trolley_position: float) -> float:
    config = cranes_config.get(crane_id)
    if not config:
        return 0.0

    from scheduler import work_orders
    order = work_orders.get(order_id)
    if not order:
        return 0.0

    lift_angle = compute_bearing(config.tower_x, config.tower_y, order.lift_x, order.lift_y)
    drop_angle = compute_bearing(config.tower_x, config.tower_y, order.drop_x, order.drop_y)

    lift_radius = math.sqrt(
        (order.lift_x - config.tower_x) ** 2 +
        (order.lift_y - config.tower_y) ** 2
    )
    drop_radius = math.sqrt(
        (order.drop_x - config.tower_x) ** 2 +
        (order.drop_y - config.tower_y) ** 2
    )

    total_distance = abs(drop_radius - lift_radius)
    if total_distance <= 0:
        return 50.0

    traveled = abs(trolley_position - lift_radius)
    traveled = min(traveled, total_distance)
    progress = (traveled / total_distance) * 100.0
    return min(max(progress, 0.0), 100.0)


def _calculate_average_rate(tracking: OrderProgressTracking) -> Optional[float]:
    history = tracking.progress_history
    if len(history) < 2:
        return None

    sample_size = min(_progress_config.rate_calculation_sample_size, len(history))
    recent = history[-sample_size:]

    total_delta = 0.0
    total_time = 0.0
    for i in range(1, len(recent)):
        delta_p = recent[i].progress_percent - recent[i - 1].progress_percent
        delta_t = recent[i].timestamp - recent[i - 1].timestamp
        if delta_t > 0 and delta_p > 0:
            total_delta += delta_p
            total_time += delta_t

    if total_time <= 0 or total_delta <= 0:
        return None

    return total_delta / total_time


def _generate_stagnation_alarm(tracking: OrderProgressTracking):
    now = time.time()
    stagnation_seconds = now - (tracking.stagnation_started_at or now)
    alarm = OrderProgressStagnationAlarm(
        alarm_id=f"STAG-{uuid.uuid4().hex[:8].upper()}",
        order_id=tracking.order_id,
        crane_id=tracking.crane_id,
        timestamp=now,
        datetime_str=datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        stagnation_seconds=round(stagnation_seconds, 1),
        current_progress=tracking.current_progress,
        message=(
            f"塔吊[{tracking.crane_id}]工单[{tracking.order_id}]执行停滞超过"
            f"{_progress_config.stagnation_alarm_timeout_seconds:.0f}秒，"
            f"当前进度{tracking.current_progress:.1f}%"
        ),
        details={
            "stagnation_threshold_seconds": _progress_config.stagnation_alarm_timeout_seconds,
            "progress_at_stagnation_start": tracking.last_progress_value,
        },
    )
    stagnation_alarms.append(alarm)
    tracking.stagnation_alarm_generated = True

    pass


def update_progress_on_status_report(crane_id: str, status: CraneStatus):
    order_id = crane_current_executing_order.get(crane_id)
    if not order_id:
        return

    tracking = active_progress_tracking.get(order_id)
    if not tracking:
        return

    from scheduler import work_orders
    order = work_orders.get(order_id)
    if not order or order.status != WorkOrderStatus.EXECUTING:
        return

    now = status.timestamp or time.time()
    current_angle = normalize_angle(status.rotation_angle)

    if tracking.has_path_plan and active_path_plans.get(order_id):
        raw_progress = _calculate_progress_by_path(order_id, current_angle, crane_id, status.trolley_position)
    else:
        raw_progress = _calculate_progress_by_distance(order_id, crane_id, status.trolley_position)

    new_progress = max(raw_progress, tracking.last_progress_value)
    delta = new_progress - tracking.last_progress_value

    if delta > 0.01:
        if tracking.is_stagnated:
            tracking.is_stagnated = False
            tracking.stagnation_started_at = None
            tracking.stagnation_alarm_generated = False
            for alarm in stagnation_alarms:
                if (alarm.order_id == order_id and
                        not alarm.resolved):
                    alarm.resolved = True
                    alarm.resolved_at = now

        record = OrderProgressUpdateRecord(
            timestamp=now,
            progress_percent=new_progress,
            delta_percent=delta,
        )
        tracking.progress_history.append(record)
        tracking.current_progress = new_progress
        tracking.last_progress_value = new_progress
        tracking.last_progress_update_at = now

        avg_rate = _calculate_average_rate(tracking)
        if avg_rate and avg_rate > 0:
            remaining_percent = 100.0 - new_progress
            tracking.estimated_remaining_seconds = round(remaining_percent / avg_rate, 1)
        else:
            tracking.estimated_remaining_seconds = tracking.estimated_remaining_seconds

        order.progress_percent = new_progress
        order.last_progress_updated_at = now
    else:
        time_since_last_update = now - (tracking.last_progress_update_at or now)
        if time_since_last_update >= _progress_config.stagnation_detect_seconds:
            if not tracking.is_stagnated:
                tracking.is_stagnated = True
                tracking.stagnation_started_at = tracking.last_progress_update_at or now

            if (tracking.is_stagnated and
                    not tracking.stagnation_alarm_generated and
                    tracking.stagnation_started_at):
                stagnation_duration = now - tracking.stagnation_started_at
                if stagnation_duration >= _progress_config.stagnation_alarm_timeout_seconds:
                    _generate_stagnation_alarm(tracking)


def complete_order_progress(order_id: str):
    tracking = active_progress_tracking.get(order_id)
    if not tracking:
        return

    now = time.time()
    tracking.current_progress = 100.0
    tracking.estimated_remaining_seconds = 0.0
    tracking.is_stagnated = False

    if tracking.stagnation_started_at:
        for alarm in stagnation_alarms:
            if alarm.order_id == order_id and not alarm.resolved:
                alarm.resolved = True
                alarm.resolved_at = now

    from scheduler import work_orders
    order = work_orders.get(order_id)
    if order and order.started_at:
        actual_duration = now - order.started_at
        estimated_duration = order.estimated_duration * 60.0
        deviation_ratio = actual_duration / estimated_duration if estimated_duration > 0 else None

        order.progress_percent = 100.0
        order.actual_duration_seconds = round(actual_duration, 1)
        order.duration_deviation_ratio = round(deviation_ratio, 3) if deviation_ratio else None
        order.last_progress_updated_at = now

    crane_id = tracking.crane_id
    if crane_current_executing_order.get(crane_id) == order_id:
        del crane_current_executing_order[crane_id]

    completed_progress_history[order_id] = tracking
    active_progress_tracking.pop(order_id, None)


def cancel_order_progress(order_id: str):
    tracking = active_progress_tracking.get(order_id)
    if not tracking:
        return

    now = time.time()
    if tracking.stagnation_started_at:
        for alarm in stagnation_alarms:
            if alarm.order_id == order_id and not alarm.resolved:
                alarm.resolved = True
                alarm.resolved_at = now

    crane_id = tracking.crane_id
    if crane_current_executing_order.get(crane_id) == order_id:
        del crane_current_executing_order[crane_id]

    active_progress_tracking.pop(order_id, None)


def get_order_progress(order_id: str) -> Optional[OrderProgressResponse]:
    tracking = active_progress_tracking.get(order_id)
    if not tracking:
        from scheduler import work_orders
        order = work_orders.get(order_id)
        if order and order.status == WorkOrderStatus.COMPLETED:
            return OrderProgressResponse(
                order_id=order_id,
                crane_id=order.assigned_crane_id or "",
                current_progress=100.0,
                estimated_remaining_seconds=0.0,
                is_stagnated=False,
                stagnation_seconds=0.0,
                last_progress_update_at=order.last_progress_updated_at,
                execution_started_at=order.started_at,
            )
        return None

    now = time.time()
    stagnation_seconds = 0.0
    if tracking.is_stagnated and tracking.stagnation_started_at:
        stagnation_seconds = round(now - tracking.stagnation_started_at, 1)

    from scheduler import work_orders
    order = work_orders.get(order_id)
    started_at = order.started_at if order else None

    return OrderProgressResponse(
        order_id=order_id,
        crane_id=tracking.crane_id,
        current_progress=round(tracking.current_progress, 1),
        estimated_remaining_seconds=tracking.estimated_remaining_seconds,
        is_stagnated=tracking.is_stagnated,
        stagnation_seconds=stagnation_seconds,
        last_progress_update_at=tracking.last_progress_update_at,
        execution_started_at=started_at,
    )


def get_crane_current_progress(crane_id: str) -> Optional[OrderProgressResponse]:
    order_id = crane_current_executing_order.get(crane_id)
    if not order_id:
        return None
    return get_order_progress(order_id)


def get_crane_deviation_ratio_stats(crane_id: str) -> OrderDeviationRatioStats:
    from scheduler import work_orders, crane_history

    history_ids = crane_history.get(crane_id, [])
    recent_ids = history_ids[-10:] if len(history_ids) > 10 else history_ids

    ratios = []
    recent_orders = []
    for oid in recent_ids:
        order = work_orders.get(oid)
        if (order and order.status == WorkOrderStatus.COMPLETED and
                order.duration_deviation_ratio is not None):
            ratios.append(order.duration_deviation_ratio)
            recent_orders.append({
                "order_id": order.order_id,
                "estimated_duration_minutes": order.estimated_duration,
                "actual_duration_seconds": order.actual_duration_seconds,
                "deviation_ratio": order.duration_deviation_ratio,
                "completed_at": order.completed_at,
            })

    avg_ratio = round(sum(ratios) / len(ratios), 3) if ratios else None

    return OrderDeviationRatioStats(
        crane_id=crane_id,
        recent_orders_count=len(recent_orders),
        avg_deviation_ratio=avg_ratio,
        recent_orders=recent_orders,
    )


def get_stagnation_alarms(crane_id: Optional[str] = None,
                          order_id: Optional[str] = None,
                          limit: int = 100) -> List[OrderProgressStagnationAlarm]:
    result = stagnation_alarms
    if crane_id:
        result = [a for a in result if a.crane_id == crane_id]
    if order_id:
        result = [a for a in result if a.order_id == order_id]
    return result[-limit:]


def count_progress_updates_for_crane(crane_id: str, start_ts: float, end_ts: float) -> int:
    count = 0
    all_trackings = list(active_progress_tracking.values()) + list(completed_progress_history.values())
    for tracking in all_trackings:
        if tracking.crane_id != crane_id:
            continue
        for record in tracking.progress_history:
            if start_ts <= record.timestamp < end_ts:
                count += 1
    return count


def count_stagnation_alarms_for_crane(crane_id: str, start_ts: float, end_ts: float) -> int:
    return sum(
        1 for a in stagnation_alarms
        if a.crane_id == crane_id and start_ts <= a.timestamp < end_ts
    )
