import time
import uuid
from typing import Dict, List, Optional

from models import (
    PathDirection,
    PathSegment,
    PathPlan,
    PathSegmentStatus,
    PathSegmentRehearsal,
    PathRehearsalResult,
    PathExecutionRecord,
)
from collision import (
    cranes_config,
    compute_bearing,
    normalize_angle,
    angle_in_interval,
)
from arbiter import (
    overlap_sectors,
    token_statuses,
    find_sectors_for_crane,
    get_crane_interval_for_sector,
    clean_expired_tokens_and_waiters,
)

ROTATION_SPEED_DEG_PER_SEC = 2.0

active_path_plans: Dict[str, PathPlan] = {}
path_execution_history: Dict[str, List[PathExecutionRecord]] = {}
pending_rehearsal_results: Dict[str, PathRehearsalResult] = {}


def _angular_distance_cw(start_angle: float, end_angle: float) -> float:
    return (end_angle - start_angle) % 360.0


def _angular_distance_ccw(start_angle: float, end_angle: float) -> float:
    return (start_angle - end_angle) % 360.0


def _angle_position_on_path(angle: float, start_angle: float, direction: PathDirection) -> float:
    if direction == PathDirection.CW:
        return (angle - start_angle) % 360.0
    return (start_angle - angle) % 360.0


def _position_to_angle(position: float, start_angle: float, direction: PathDirection) -> float:
    if direction == PathDirection.CW:
        return normalize_angle(start_angle + position)
    return normalize_angle(start_angle - position)


def _choose_main_direction(lift_angle: float, drop_angle: float) -> PathDirection:
    cw = _angular_distance_cw(lift_angle, drop_angle)
    ccw = _angular_distance_ccw(lift_angle, drop_angle)
    if cw <= ccw:
        return PathDirection.CW
    return PathDirection.CCW


def plan_path(crane_id: str,
              lift_x: float, lift_y: float,
              drop_x: float, drop_y: float,
              direction: PathDirection,
              order_id: str = "") -> PathPlan:
    config = cranes_config.get(crane_id)
    if not config:
        raise ValueError(f"塔吊 {crane_id} 不存在")

    lift_angle = compute_bearing(config.tower_x, config.tower_y, lift_x, lift_y)
    drop_angle = compute_bearing(config.tower_x, config.tower_y, drop_x, drop_y)

    if direction == PathDirection.CW:
        sweep = _angular_distance_cw(lift_angle, drop_angle)
    else:
        sweep = _angular_distance_ccw(lift_angle, drop_angle)

    sectors = find_sectors_for_crane(crane_id)

    if sweep == 0:
        active_sector_ids = []
        for sector in sectors:
            interval = get_crane_interval_for_sector(sector, crane_id)
            if interval and angle_in_interval(lift_angle, interval):
                active_sector_ids.append(sector.sector_id)

        segment = PathSegment(
            segment_index=0,
            start_angle=round(lift_angle, 2),
            end_angle=round(lift_angle, 2),
            sector_ids=sorted(active_sector_ids),
            required_tokens=sorted(active_sector_ids),
            estimated_time_seconds=0.0,
        )

        plan = PathPlan(
            plan_id=f"PATH-{uuid.uuid4().hex[:8].upper()}",
            order_id=order_id,
            crane_id=crane_id,
            lift_angle=round(lift_angle, 2),
            drop_angle=round(drop_angle, 2),
            direction=direction,
            segments=[segment],
            total_estimated_time=0.0,
            angular_distance=0.0,
            created_at=time.time(),
        )

        if order_id:
            active_path_plans[order_id] = plan

        return plan

    all_boundary_angles = set()
    for sector in sectors:
        interval = get_crane_interval_for_sector(sector, crane_id)
        if not interval:
            continue
        all_boundary_angles.add(normalize_angle(interval.start))
        all_boundary_angles.add(normalize_angle(interval.end))

    path_boundaries = []
    for b_angle in all_boundary_angles:
        pos = _angle_position_on_path(b_angle, lift_angle, direction)
        if 0 < pos < sweep:
            path_boundaries.append((pos, b_angle))

    path_boundaries.append((0.0, lift_angle))
    path_boundaries.append((sweep, drop_angle))

    path_boundaries.sort(key=lambda x: x[0])

    unique_boundaries = []
    seen_positions = set()
    for pos, angle in path_boundaries:
        rp = round(pos, 4)
        if rp not in seen_positions:
            seen_positions.add(rp)
            unique_boundaries.append((pos, angle))

    segments = []
    for i in range(len(unique_boundaries) - 1):
        pos_start = unique_boundaries[i][0]
        pos_end = unique_boundaries[i + 1][0]
        angle_start = unique_boundaries[i][1]
        angle_end = unique_boundaries[i + 1][1]

        mid_pos = (pos_start + pos_end) / 2.0
        mid_angle = _position_to_angle(mid_pos, lift_angle, direction)

        active_sector_ids = []
        for sector in sectors:
            interval = get_crane_interval_for_sector(sector, crane_id)
            if interval and angle_in_interval(mid_angle, interval):
                active_sector_ids.append(sector.sector_id)

        seg_sweep = pos_end - pos_start
        estimated_time = seg_sweep / ROTATION_SPEED_DEG_PER_SEC

        segments.append(PathSegment(
            segment_index=i,
            start_angle=round(angle_start, 2),
            end_angle=round(angle_end, 2),
            sector_ids=sorted(active_sector_ids),
            required_tokens=sorted(active_sector_ids),
            estimated_time_seconds=round(estimated_time, 2),
        ))

    total_time = sum(s.estimated_time_seconds for s in segments)

    plan = PathPlan(
        plan_id=f"PATH-{uuid.uuid4().hex[:8].upper()}",
        order_id=order_id,
        crane_id=crane_id,
        lift_angle=round(lift_angle, 2),
        drop_angle=round(drop_angle, 2),
        direction=direction,
        segments=segments,
        total_estimated_time=round(total_time, 2),
        angular_distance=round(sweep, 2),
        created_at=time.time(),
    )

    if order_id:
        active_path_plans[order_id] = plan

    return plan


def _rehearse_plan(plan: PathPlan, crane_id: str) -> List[PathSegmentRehearsal]:
    clean_expired_tokens_and_waiters()
    now = time.time()
    config = cranes_config.get(crane_id)
    result = []

    for seg in plan.segments:
        status = PathSegmentStatus.CLEAR
        conflict_holder = None
        wait_time = 0.0

        for token_id in seg.required_tokens:
            ts = token_statuses.get(token_id)
            if ts and ts.holder_crane_id and ts.holder_crane_id != crane_id:
                status = PathSegmentStatus.CONFLICT
                if conflict_holder is None:
                    conflict_holder = ts.holder_crane_id
                remaining = max(0.0, (ts.expires_at or 0) - now)
                wait_time = max(wait_time, remaining)

        if config and status != PathSegmentStatus.CONFLICT:
            for check_angle in [seg.start_angle, seg.end_angle]:
                if check_angle < config.min_angle or check_angle > config.max_angle:
                    status = PathSegmentStatus.UNREACHABLE
                    break

        result.append(PathSegmentRehearsal(
            segment_index=seg.segment_index,
            start_angle=seg.start_angle,
            end_angle=seg.end_angle,
            sector_ids=seg.sector_ids,
            required_tokens=seg.required_tokens,
            estimated_time_seconds=seg.estimated_time_seconds,
            status=status,
            conflict_token_holder=conflict_holder,
            estimated_wait_seconds=round(wait_time, 2),
        ))

    return result


def rehearse_path_for_order(order_id: str) -> PathRehearsalResult:
    from scheduler import work_orders

    order = work_orders.get(order_id)
    if not order:
        raise ValueError(f"工单 {order_id} 不存在")
    if not order.assigned_crane_id:
        raise ValueError(f"工单 {order_id} 未分配塔吊")

    crane_id = order.assigned_crane_id
    main_direction = _choose_main_direction(
        *compute_lift_drop_angles(crane_id, order.lift_x, order.lift_y,
                                  order.drop_x, order.drop_y)
    )

    main_plan = plan_path(
        crane_id, order.lift_x, order.lift_y,
        order.drop_x, order.drop_y,
        main_direction, order_id,
    )

    main_rehearsal = _rehearse_plan(main_plan, crane_id)
    has_conflict = any(s.status == PathSegmentStatus.CONFLICT for s in main_rehearsal)
    has_unreachable = any(s.status == PathSegmentStatus.UNREACHABLE for s in main_rehearsal)

    main_total_time = sum(
        s.estimated_time_seconds + s.estimated_wait_seconds for s in main_rehearsal
    )

    result = PathRehearsalResult(
        order_id=order_id,
        crane_id=crane_id,
        main_path=main_rehearsal,
        main_path_total_time=round(main_total_time, 2),
        has_conflict=has_conflict or has_unreachable,
    )

    if has_conflict:
        alt_direction = PathDirection.CCW if main_direction == PathDirection.CW else PathDirection.CW
        alt_plan = plan_path(
            crane_id, order.lift_x, order.lift_y,
            order.drop_x, order.drop_y,
            alt_direction,
        )
        alt_rehearsal = _rehearse_plan(alt_plan, crane_id)
        alt_has_conflict = any(
            s.status in (PathSegmentStatus.CONFLICT, PathSegmentStatus.UNREACHABLE)
            for s in alt_rehearsal
        )

        if not alt_has_conflict:
            alt_total_time = sum(
                s.estimated_time_seconds + s.estimated_wait_seconds for s in alt_rehearsal
            )
            result.alternative_path = alt_rehearsal
            result.alternative_direction = alt_direction
            result.alternative_path_total_time = round(alt_total_time, 2)

    pending_rehearsal_results[order_id] = result
    return result


def compute_lift_drop_angles(crane_id: str,
                             lift_x: float, lift_y: float,
                             drop_x: float, drop_y: float):
    config = cranes_config.get(crane_id)
    if not config:
        raise ValueError(f"塔吊 {crane_id} 不存在")
    lift_angle = compute_bearing(config.tower_x, config.tower_y, lift_x, lift_y)
    drop_angle = compute_bearing(config.tower_x, config.tower_y, drop_x, drop_y)
    return lift_angle, drop_angle


def collect_path_sector_ids(plan: PathPlan) -> List[str]:
    seen = set()
    result = []
    for seg in plan.segments:
        for sid in seg.required_tokens:
            if sid not in seen:
                seen.add(sid)
                result.append(sid)
    return result


def record_path_execution(order_id: str, plan: PathPlan,
                          started_at: float, completed_at: float) -> PathExecutionRecord:
    actual_time = completed_at - started_at
    estimated_time = plan.total_estimated_time
    deviation = actual_time - estimated_time

    record = PathExecutionRecord(
        record_id=f"REC-{uuid.uuid4().hex[:8].upper()}",
        plan_id=plan.plan_id,
        order_id=order_id,
        crane_id=plan.crane_id,
        direction=plan.direction,
        estimated_total_time=round(estimated_time, 2),
        actual_total_time=round(actual_time, 2),
        deviation_seconds=round(deviation, 2),
        segment_count=len(plan.segments),
        executed_at=started_at,
        completed_at=completed_at,
    )

    if plan.crane_id not in path_execution_history:
        path_execution_history[plan.crane_id] = []
    path_execution_history[plan.crane_id].append(record)

    active_path_plans.pop(order_id, None)
    pending_rehearsal_results.pop(order_id, None)

    return record


def get_active_plans_for_crane(crane_id: str) -> List[PathPlan]:
    return [p for p in active_path_plans.values() if p.crane_id == crane_id]


def get_execution_history_for_crane(crane_id: str) -> List[PathExecutionRecord]:
    return path_execution_history.get(crane_id, [])


def init_path_planner():
    active_path_plans.clear()
    path_execution_history.clear()
    pending_rehearsal_results.clear()
