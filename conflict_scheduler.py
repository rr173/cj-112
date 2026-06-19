import math
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from models import (
    WorkOrderForConflictCheck,
    ConflictPoint,
    ConflictSuggestion,
    ConflictSuggestionType,
    ConflictDetectionReport,
    ConflictDetectionReportStatus,
    WorkOrderPriority,
)
from collision import (
    cranes_config,
    can_crane_reach_point,
    compute_bearing,
    normalize_angle,
    angle_in_interval,
)
from arbiter import (
    overlap_sectors,
    rebuild_all_overlap_sectors,
)
from scheduler import can_crane_cover, _find_best_crane


conflict_detection_reports: Dict[str, ConflictDetectionReport] = {}
conflict_detection_history: List[ConflictDetectionReport] = []


def init_conflict_scheduler_module():
    if not overlap_sectors:
        rebuild_all_overlap_sectors()


def _get_order_work_sector_ids(order: WorkOrderForConflictCheck, crane_id: str) -> List[str]:
    config = cranes_config.get(crane_id)
    if not config:
        return []

    lift_bearing = compute_bearing(config.tower_x, config.tower_y, order.lift_x, order.lift_y)
    drop_bearing = compute_bearing(config.tower_x, config.tower_y, order.drop_x, order.drop_y)

    lift_bearing = normalize_angle(lift_bearing)
    drop_bearing = normalize_angle(drop_bearing)

    sector_ids = []
    for sector_id, sector in overlap_sectors.items():
        if sector.crane_a_id == crane_id:
            interval = sector.crane_a_interval
        elif sector.crane_b_id == crane_id:
            interval = sector.crane_b_interval
        else:
            continue

        if angle_in_interval(lift_bearing, interval) or angle_in_interval(drop_bearing, interval):
            sector_ids.append(sector_id)
            continue

        s, e = interval.start, interval.end
        if interval.wraps_zero:
            if lift_bearing >= s or drop_bearing <= e:
                sector_ids.append(sector_id)
            elif drop_bearing >= s or lift_bearing <= e:
                sector_ids.append(sector_id)
        else:
            min_angle = min(lift_bearing, drop_bearing)
            max_angle = max(lift_bearing, drop_bearing)
            if max_angle >= s and min_angle <= e:
                sector_ids.append(sector_id)

    return sector_ids


def _check_time_overlap(start1: float, end1: float, start2: float, end2: float) -> bool:
    return start1 < end2 and start2 < end1


def _assign_crane_to_order(order: WorkOrderForConflictCheck) -> Optional[str]:
    if order.assigned_crane_id:
        if can_crane_cover(order.assigned_crane_id, order.lift_x, order.lift_y,
                           order.drop_x, order.drop_y, order.weight):
            return order.assigned_crane_id

    best_crane = _find_best_crane(
        order.lift_x, order.lift_y,
        order.drop_x, order.drop_y,
        order.weight,
        order.priority,
    )
    return best_crane


def _generate_delay_suggestion(
    conflict: ConflictPoint,
    orders_map: Dict[str, WorkOrderForConflictCheck],
    all_orders: List[WorkOrderForConflictCheck],
) -> Optional[ConflictSuggestion]:
    order_a = orders_map.get(conflict.order_a_id)
    order_b = orders_map.get(conflict.order_b_id)
    if not order_a or not order_b:
        return None

    end_a = order_a.planned_start_time + order_a.estimated_duration * 60
    end_b = order_b.planned_start_time + order_b.estimated_duration * 60

    delay_a = max(0, end_b - order_a.planned_start_time)
    delay_b = max(0, end_a - order_b.planned_start_time)

    if delay_a <= delay_b:
        delay_minutes = delay_a / 60
        target_order_id = conflict.order_a_id
        new_start = order_b.planned_start_time + order_b.estimated_duration * 60
        description = f"延后工单[{conflict.order_a_id}]至塔吊[{conflict.crane_b_id}]完成后开始，延迟约{delay_minutes:.1f}分钟"
        details = {
            "delayed_order_id": conflict.order_a_id,
            "new_planned_start_time": new_start,
            "delay_minutes": delay_minutes,
            "reason": f"避开与工单[{conflict.order_b_id}]的时间冲突",
        }
    else:
        delay_minutes = delay_b / 60
        target_order_id = conflict.order_b_id
        new_start = order_a.planned_start_time + order_a.estimated_duration * 60
        description = f"延后工单[{conflict.order_b_id}]至塔吊[{conflict.crane_a_id}]完成后开始，延迟约{delay_minutes:.1f}分钟"
        details = {
            "delayed_order_id": conflict.order_b_id,
            "new_planned_start_time": new_start,
            "delay_minutes": delay_minutes,
            "reason": f"避开与工单[{conflict.order_a_id}]的时间冲突",
        }

    return ConflictSuggestion(
        suggestion_id=str(uuid.uuid4()),
        suggestion_type=ConflictSuggestionType.DELAY_ORDER,
        description=description,
        total_delay_minutes=delay_minutes,
        affected_order_ids=[target_order_id],
        details=details,
    )


def _generate_change_crane_suggestion(
    conflict: ConflictPoint,
    orders_map: Dict[str, WorkOrderForConflictCheck],
    all_orders: List[WorkOrderForConflictCheck],
) -> Optional[ConflictSuggestion]:
    order_a = orders_map.get(conflict.order_a_id)
    order_b = orders_map.get(conflict.order_b_id)
    if not order_a or not order_b:
        return None

    current_crane_a = conflict.crane_a_id
    current_crane_b = conflict.crane_b_id

    other_cranes_a = []
    other_cranes_b = []

    for cid in cranes_config:
        if cid == current_crane_a:
            continue
        if can_crane_cover(cid, order_a.lift_x, order_a.lift_y,
                           order_a.drop_x, order_a.drop_y, order_a.weight):
            other_cranes_a.append(cid)

    for cid in cranes_config:
        if cid == current_crane_b:
            continue
        if can_crane_cover(cid, order_b.lift_x, order_b.lift_y,
                           order_b.drop_x, order_b.drop_y, order_b.weight):
            other_cranes_b.append(cid)

    best_option = None
    min_delay = float("inf")

    for cid in other_cranes_a:
        existing_orders_on_crane = [
            o for o in all_orders
            if o.assigned_crane_id == cid and o.order_id != order_a.order_id
        ]
        delay = _calculate_crane_switch_delay(order_a, existing_orders_on_crane, cid)
        if delay < min_delay:
            min_delay = delay
            best_option = {
                "order_id": conflict.order_a_id,
                "old_crane": current_crane_a,
                "new_crane": cid,
                "delay_minutes": delay,
                "description": f"将工单[{conflict.order_a_id}]从塔吊[{current_crane_a}]调换至[{cid}]，延迟约{delay:.1f}分钟",
                "details": {
                    "changed_order_id": conflict.order_a_id,
                    "old_crane_id": current_crane_a,
                    "new_crane_id": cid,
                    "delay_minutes": delay,
                    "reason": f"避开与塔吊[{current_crane_b}]上工单[{conflict.order_b_id}]的重叠扇区冲突",
                },
            }

    for cid in other_cranes_b:
        existing_orders_on_crane = [
            o for o in all_orders
            if o.assigned_crane_id == cid and o.order_id != order_b.order_id
        ]
        delay = _calculate_crane_switch_delay(order_b, existing_orders_on_crane, cid)
        if delay < min_delay:
            min_delay = delay
            best_option = {
                "order_id": conflict.order_b_id,
                "old_crane": current_crane_b,
                "new_crane": cid,
                "delay_minutes": delay,
                "description": f"将工单[{conflict.order_b_id}]从塔吊[{current_crane_b}]调换至[{cid}]，延迟约{delay:.1f}分钟",
                "details": {
                    "changed_order_id": conflict.order_b_id,
                    "old_crane_id": current_crane_b,
                    "new_crane_id": cid,
                    "delay_minutes": delay,
                    "reason": f"避开与塔吊[{current_crane_a}]上工单[{conflict.order_a_id}]的重叠扇区冲突",
                },
            }

    if best_option is None:
        return None

    return ConflictSuggestion(
        suggestion_id=str(uuid.uuid4()),
        suggestion_type=ConflictSuggestionType.CHANGE_CRANE,
        description=best_option["description"],
        total_delay_minutes=best_option["delay_minutes"],
        affected_order_ids=[best_option["order_id"]],
        details=best_option["details"],
    )


def _calculate_crane_switch_delay(
    order: WorkOrderForConflictCheck,
    existing_orders: List[WorkOrderForConflictCheck],
    crane_id: str,
) -> float:
    if not existing_orders:
        return 0

    order_end = order.planned_start_time + order.estimated_duration * 60

    latest_end = 0
    for eo in existing_orders:
        eo_end = eo.planned_start_time + eo.estimated_duration * 60
        if _check_time_overlap(order.planned_start_time, order_end,
                               eo.planned_start_time, eo_end):
            if eo_end > latest_end:
                latest_end = eo_end

    if latest_end == 0:
        return 0

    delay_seconds = max(0, latest_end - order.planned_start_time)
    return delay_seconds / 60


def _generate_split_order_suggestion(
    conflict: ConflictPoint,
    orders_map: Dict[str, WorkOrderForConflictCheck],
    all_orders: List[WorkOrderForConflictCheck],
) -> Optional[ConflictSuggestion]:
    order_a = orders_map.get(conflict.order_a_id)
    order_b = orders_map.get(conflict.order_b_id)
    if not order_a or not order_b:
        return None

    start_a = order_a.planned_start_time
    end_a = start_a + order_a.estimated_duration * 60
    start_b = order_b.planned_start_time
    end_b = start_b + order_b.estimated_duration * 60

    overlap_start = max(start_a, start_b)
    overlap_end = min(end_a, end_b)
    overlap_duration = max(0, overlap_end - overlap_start)

    if overlap_duration <= 0:
        return None

    total_duration = order_a.estimated_duration * 60
    non_overlap_before = max(0, overlap_start - start_a)
    non_overlap_after = max(0, end_a - overlap_end)

    split_ratio_1 = non_overlap_before / total_duration if total_duration > 0 else 0
    split_ratio_2 = non_overlap_after / total_duration if total_duration > 0 else 0

    duration_1 = split_ratio_1 * order_a.estimated_duration
    duration_2 = split_ratio_2 * order_a.estimated_duration

    delay_minutes = (overlap_duration + 60) / 60

    description = (
        f"拆分工单[{conflict.order_a_id}]为两段执行："
        f"第一段{duration_1:.1f}分钟保留原计划，"
        f"第二段{duration_2:.1f}分钟延后至冲突结束后执行，"
        f"总延迟约{delay_minutes:.1f}分钟"
    )

    details = {
        "split_order_id": conflict.order_a_id,
        "segment_1": {
            "duration_minutes": duration_1,
            "planned_start_time": start_a,
        },
        "segment_2": {
            "duration_minutes": duration_2,
            "planned_start_time": overlap_end + 60,
        },
        "gap_minutes": 1,
        "delay_minutes": delay_minutes,
        "reason": f"避开与工单[{conflict.order_b_id}]在重叠扇区的作业时间冲突",
    }

    return ConflictSuggestion(
        suggestion_id=str(uuid.uuid4()),
        suggestion_type=ConflictSuggestionType.SPLIT_ORDER,
        description=description,
        total_delay_minutes=delay_minutes,
        affected_order_ids=[conflict.order_a_id],
        details=details,
    )


def _generate_suggestions(
    conflict: ConflictPoint,
    orders_map: Dict[str, WorkOrderForConflictCheck],
    all_orders: List[WorkOrderForConflictCheck],
) -> List[ConflictSuggestion]:
    suggestions = []

    delay_suggestion = _generate_delay_suggestion(conflict, orders_map, all_orders)
    if delay_suggestion:
        suggestions.append(delay_suggestion)

    change_crane_suggestion = _generate_change_crane_suggestion(conflict, orders_map, all_orders)
    if change_crane_suggestion:
        suggestions.append(change_crane_suggestion)

    split_suggestion = _generate_split_order_suggestion(conflict, orders_map, all_orders)
    if split_suggestion:
        suggestions.append(split_suggestion)

    suggestions.sort(key=lambda s: s.total_delay_minutes)
    return suggestions


def detect_conflicts(
    orders: List[WorkOrderForConflictCheck],
    submitter: Optional[str] = None,
) -> ConflictDetectionReport:
    now = time.time()
    now_str = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")

    processed_orders = []
    orders_map: Dict[str, WorkOrderForConflictCheck] = {}
    crane_assignments: Dict[str, List[WorkOrderForConflictCheck]] = {}
    order_sectors: Dict[str, List[str]] = {}

    for order in orders:
        if order.order_id is None:
            order.order_id = f"WO-CONF-{uuid.uuid4().hex[:6].upper()}"

        assigned_crane = _assign_crane_to_order(order)
        if assigned_crane:
            order.assigned_crane_id = assigned_crane

        if order.assigned_crane_id:
            if order.assigned_crane_id not in crane_assignments:
                crane_assignments[order.assigned_crane_id] = []
            crane_assignments[order.assigned_crane_id].append(order)
            sector_ids = _get_order_work_sector_ids(order, order.assigned_crane_id)
            order_sectors[order.order_id] = sector_ids

        processed_orders.append(order)
        orders_map[order.order_id] = order

    conflicts: List[ConflictPoint] = []
    seen_conflicts = set()

    crane_ids = list(crane_assignments.keys())
    for i in range(len(crane_ids)):
        for j in range(i + 1, len(crane_ids)):
            crane_a_id = crane_ids[i]
            crane_b_id = crane_ids[j]

            orders_a = crane_assignments[crane_a_id]
            orders_b = crane_assignments[crane_b_id]

            for order_a in orders_a:
                for order_b in orders_b:
                    sectors_a = order_sectors.get(order_a.order_id, [])
                    sectors_b = order_sectors.get(order_b.order_id, [])

                    common_sectors = set(sectors_a) & set(sectors_b)
                    if not common_sectors:
                        continue

                    start_a = order_a.planned_start_time
                    end_a = start_a + order_a.estimated_duration * 60
                    start_b = order_b.planned_start_time
                    end_b = start_b + order_b.estimated_duration * 60

                    if not _check_time_overlap(start_a, end_a, start_b, end_b):
                        continue

                    for sector_id in common_sectors:
                        conflict_key = (
                            sector_id,
                            min(order_a.order_id, order_b.order_id),
                            max(order_a.order_id, order_b.order_id),
                        )
                        if conflict_key in seen_conflicts:
                            continue
                        seen_conflicts.add(conflict_key)

                        overlap_start = max(start_a, start_b)
                        overlap_end = min(end_a, end_b)

                        priority_a = order_a.priority.value if hasattr(order_a.priority, 'value') else str(order_a.priority)
                        priority_b = order_b.priority.value if hasattr(order_b.priority, 'value') else str(order_b.priority)
                        if priority_a == "URGENT" or priority_b == "URGENT":
                            severity = "HIGH"
                        elif priority_a == "LOW" and priority_b == "LOW":
                            severity = "LOW"
                        else:
                            severity = "MEDIUM"

                        conflict = ConflictPoint(
                            conflict_id=str(uuid.uuid4()),
                            conflict_time_start=overlap_start,
                            conflict_time_end=overlap_end,
                            overlapping_sector_id=sector_id,
                            crane_a_id=crane_a_id,
                            crane_b_id=crane_b_id,
                            order_a_id=order_a.order_id,
                            order_b_id=order_b.order_id,
                            severity=severity,
                        )
                        conflict.suggestions = _generate_suggestions(conflict, orders_map, processed_orders)
                        conflicts.append(conflict)

    total_cranes = len(set(
        [o.assigned_crane_id for o in processed_orders if o.assigned_crane_id]
    ))

    report = ConflictDetectionReport(
        report_id=str(uuid.uuid4()),
        submitted_at=now,
        submitted_datetime_str=now_str,
        total_orders=len(processed_orders),
        total_cranes_involved=total_cranes,
        conflict_count=len(conflicts),
        status=ConflictDetectionReportStatus.COMPLETED,
        conflicts=conflicts,
        checked_orders=processed_orders,
        remarks=f"由{submitter}提交" if submitter else "系统自动检测",
    )

    conflict_detection_reports[report.report_id] = report
    conflict_detection_history.append(report)

    try:
        from daily_report import add_conflict_detection_record
        add_conflict_detection_record(
            report.report_id,
            len(conflicts),
            0,
            now,
        )
    except ImportError:
        pass

    return report


def accept_suggestion(
    report_id: str,
    conflict_id: str,
    suggestion_id: str,
    accepted_by: str,
    remarks: Optional[str] = None,
) -> Dict:
    report = conflict_detection_reports.get(report_id)
    if not report:
        return {"error": f"冲突检测报告 {report_id} 不存在"}

    target_conflict = None
    target_suggestion = None

    for conflict in report.conflicts:
        if conflict.conflict_id == conflict_id:
            target_conflict = conflict
            for suggestion in conflict.suggestions:
                if suggestion.suggestion_id == suggestion_id:
                    target_suggestion = suggestion
                    break
            break

    if not target_conflict:
        return {"error": f"冲突点 {conflict_id} 不存在"}

    if not target_suggestion:
        return {"error": f"建议 {suggestion_id} 不存在"}

    if target_suggestion.is_accepted:
        return {"error": "该建议已被接受，不可重复操作"}

    now = time.time()
    target_suggestion.is_accepted = True
    target_suggestion.accepted_at = now
    target_suggestion.accepted_by = accepted_by

    adjustment_result = _apply_suggestion_adjustment(
        report, target_conflict, target_suggestion
    )

    if "error" in adjustment_result:
        target_suggestion.is_accepted = False
        target_suggestion.accepted_at = None
        target_suggestion.accepted_by = None
        return adjustment_result

    report.adjusted_orders.append(adjustment_result)
    report.accepted_suggestion_count += 1

    unresolved_conflicts = [
        c for c in report.conflicts
        if not any(s.is_accepted for s in c.suggestions)
    ]
    if not unresolved_conflicts:
        report.status = ConflictDetectionReportStatus.RESOLVED
    elif report.accepted_suggestion_count > 0:
        report.status = ConflictDetectionReportStatus.PARTIALLY_RESOLVED

    resolved_count = sum(
        1 for c in report.conflicts
        if any(s.is_accepted for s in c.suggestions)
    )
    report.resolved_conflict_count = resolved_count

    try:
        from daily_report import update_conflict_detection_accepted
        update_conflict_detection_accepted(report.report_id, now)
    except ImportError:
        pass

    try:
        from scheduler import work_orders, _assign_order_to_crane
        for adjusted in adjustment_result.get("adjusted_orders", []):
            order_id = adjusted.get("order_id")
            if order_id and order_id in work_orders:
                order = work_orders[order_id]
                new_start = adjusted.get("new_planned_start_time")
                new_crane = adjusted.get("new_crane_id")
                if new_start:
                    order.assigned_at = new_start
                if new_crane and order.assigned_crane_id != new_crane:
                    old_crane = order.assigned_crane_id
                    if old_crane:
                        from scheduler import crane_queues
                        from collections import deque
                        if old_crane in crane_queues:
                            crane_queues[old_crane] = deque(
                                oid for oid in crane_queues[old_crane] if oid != order_id
                            )
                    _assign_order_to_crane(order_id, new_crane)
                order.updated_at = now
    except ImportError:
        pass

    return {
        "success": True,
        "message": f"建议已接受，工单调整完成",
        "adjustment": adjustment_result,
        "report": report,
    }


def _apply_suggestion_adjustment(
    report: ConflictDetectionReport,
    conflict: ConflictPoint,
    suggestion: ConflictSuggestion,
) -> Dict:
    adjusted_orders = []

    if suggestion.suggestion_type == ConflictSuggestionType.DELAY_ORDER:
        delayed_order_id = suggestion.details.get("delayed_order_id")
        new_start_time = suggestion.details.get("new_planned_start_time")

        for order in report.checked_orders:
            if order.order_id == delayed_order_id:
                old_start = order.planned_start_time
                order.planned_start_time = new_start_time
                adjusted_orders.append({
                    "order_id": delayed_order_id,
                    "adjustment_type": "DELAY",
                    "old_planned_start_time": old_start,
                    "new_planned_start_time": new_start_time,
                    "delay_minutes": suggestion.total_delay_minutes,
                })
                break

    elif suggestion.suggestion_type == ConflictSuggestionType.CHANGE_CRANE:
        changed_order_id = suggestion.details.get("changed_order_id")
        new_crane_id = suggestion.details.get("new_crane_id")
        old_crane_id = suggestion.details.get("old_crane_id")

        for order in report.checked_orders:
            if order.order_id == changed_order_id:
                order.assigned_crane_id = new_crane_id
                adjusted_orders.append({
                    "order_id": changed_order_id,
                    "adjustment_type": "CHANGE_CRANE",
                    "old_crane_id": old_crane_id,
                    "new_crane_id": new_crane_id,
                    "delay_minutes": suggestion.total_delay_minutes,
                })
                break

    elif suggestion.suggestion_type == ConflictSuggestionType.SPLIT_ORDER:
        split_order_id = suggestion.details.get("split_order_id")
        segment_1 = suggestion.details.get("segment_1", {})
        segment_2 = suggestion.details.get("segment_2", {})

        for idx, order in enumerate(report.checked_orders):
            if order.order_id == split_order_id:
                adjusted_orders.append({
                    "order_id": split_order_id,
                    "adjustment_type": "SPLIT",
                    "segment_1": {
                        "order_id": f"{split_order_id}-P1",
                        "duration_minutes": segment_1.get("duration_minutes"),
                        "planned_start_time": segment_1.get("planned_start_time"),
                        "preserves_original": True,
                    },
                    "segment_2": {
                        "order_id": f"{split_order_id}-P2",
                        "duration_minutes": segment_2.get("duration_minutes"),
                        "planned_start_time": segment_2.get("planned_start_time"),
                        "preserves_original": False,
                    },
                    "gap_minutes": suggestion.details.get("gap_minutes", 1),
                    "delay_minutes": suggestion.total_delay_minutes,
                })
                break

    if not adjusted_orders:
        return {"error": "未找到需要调整的工单"}

    return {
        "suggestion_id": suggestion.suggestion_id,
        "suggestion_type": suggestion.suggestion_type.value,
        "accepted_at": suggestion.accepted_at,
        "accepted_by": suggestion.accepted_by,
        "adjusted_orders": adjusted_orders,
        "remarks": suggestion.details.get("reason", ""),
    }


def get_conflict_report(report_id: str) -> Optional[ConflictDetectionReport]:
    return conflict_detection_reports.get(report_id)


def get_conflict_reports(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    status: Optional[ConflictDetectionReportStatus] = None,
) -> List[ConflictDetectionReport]:
    reports = list(conflict_detection_history)

    if status:
        reports = [r for r in reports if r.status == status]

    if start_date:
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            start_ts = start_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            reports = [r for r in reports if r.submitted_at >= start_ts]
        except ValueError:
            pass

    if end_date:
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            end_ts = (end_dt.replace(hour=0, minute=0, second=0, microsecond=0) +
                      timedelta(days=1)).timestamp()
            reports = [r for r in reports if r.submitted_at < end_ts]
        except ValueError:
            pass

    reports.sort(key=lambda r: r.submitted_at, reverse=True)
    return reports


def get_conflict_detection_daily_stats(date_str: str) -> Dict:
    from daily_report import get_date_range
    start_ts, end_ts = get_date_range(date_str)

    day_reports = [
        r for r in conflict_detection_history
        if start_ts <= r.submitted_at < end_ts
    ]

    total_detections = len(day_reports)
    total_conflicts = sum(r.conflict_count for r in day_reports)
    total_accepted = sum(r.accepted_suggestion_count for r in day_reports)
    report_ids = [r.report_id for r in day_reports]

    return {
        "detection_count": total_detections,
        "total_conflict_count": total_conflicts,
        "accepted_suggestion_count": total_accepted,
        "detection_report_ids": report_ids,
    }
