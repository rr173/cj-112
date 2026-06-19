import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from models import (
    DailyReport,
    DailyReportStatus,
    DailyReportDataStatus,
    DailyReportSummaryItem,
    DailyReportSummaryResponse,
    AlarmStats,
    FreezeLockStats,
    TokenStats,
    AlarmType,
    WorkOrderStatus,
    EventType,
    CraneStatusRecord,
    EmergencyDailyStats,
    EmergencyLevel,
    ConflictDetectionDailyStats,
)
from collision import cranes_config, alarm_history
from arbiter import arb_event_logs
from scheduler import work_orders


status_report_history: Dict[str, List[CraneStatusRecord]] = {}


freeze_lock_history: List[Dict] = []


daily_reports: Dict[str, DailyReport] = {}


conflict_detection_history: List[Dict] = []


def init_daily_report_module():
    for crane_id in cranes_config.keys():
        if crane_id not in status_report_history:
            status_report_history[crane_id] = []


def add_status_report_to_history(record: CraneStatusRecord):
    if record.crane_id not in status_report_history:
        status_report_history[record.crane_id] = []
    status_report_history[record.crane_id].append(record)


def add_freeze_lock_record(crane_id: str, action_type: str, action: str,
                           timestamp: float, reason: Optional[str] = None,
                           end_timestamp: Optional[float] = None):
    freeze_lock_history.append({
        "crane_id": crane_id,
        "action_type": action_type,
        "action": action,
        "timestamp": timestamp,
        "reason": reason,
        "end_timestamp": end_timestamp,
    })


def get_date_range(date_str: str) -> Tuple[float, float]:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    start_dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = start_dt + timedelta(days=1)
    return start_dt.timestamp(), end_dt.timestamp()


def get_today_date_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def add_conflict_detection_record(report_id: str, conflict_count: int,
                                  accepted_count: int, timestamp: float):
    conflict_detection_history.append({
        "report_id": report_id,
        "conflict_count": conflict_count,
        "accepted_count": accepted_count,
        "timestamp": timestamp,
    })


def update_conflict_detection_accepted(report_id: str, timestamp: float):
    for record in conflict_detection_history:
        if record["report_id"] == report_id:
            record["accepted_count"] += 1
            record["last_updated_at"] = timestamp
            break


def count_conflict_detection(start_ts: float, end_ts: float) -> ConflictDetectionDailyStats:
    stats = ConflictDetectionDailyStats()
    day_records = [
        r for r in conflict_detection_history
        if start_ts <= r["timestamp"] < end_ts
    ]
    stats.detection_count = len(day_records)
    stats.total_conflict_count = sum(r["conflict_count"] for r in day_records)
    stats.accepted_suggestion_count = sum(r["accepted_count"] for r in day_records)
    stats.detection_report_ids = [r["report_id"] for r in day_records]
    return stats


def count_completed_orders(crane_id: str, start_ts: float, end_ts: float) -> int:
    count = 0
    for order in work_orders.values():
        if (order.assigned_crane_id == crane_id and
                order.status == WorkOrderStatus.COMPLETED and
                order.completed_at is not None and
                start_ts <= order.completed_at < end_ts):
            count += 1
    return count


def get_incomplete_orders(crane_id: str, end_ts: float) -> List[str]:
    incomplete = []
    for order in work_orders.values():
        if (order.assigned_crane_id == crane_id and
                order.status == WorkOrderStatus.EXECUTING and
                order.started_at is not None and
                order.started_at < end_ts):
            incomplete.append(order.order_id)
    return incomplete


def count_status_reports(crane_id: str, start_ts: float, end_ts: float) -> Tuple[int, Optional[float], Optional[float]]:
    reports = status_report_history.get(crane_id, [])
    day_reports = [r for r in reports if start_ts <= r.timestamp < end_ts]
    if not day_reports:
        return 0, None, None
    first_ts = min(r.timestamp for r in day_reports)
    last_ts = max(r.timestamp for r in day_reports)
    return len(day_reports), first_ts, last_ts


def count_alarms(crane_id: str, start_ts: float, end_ts: float) -> AlarmStats:
    stats = AlarmStats()
    for alarm in alarm_history:
        if not (start_ts <= alarm.timestamp < end_ts):
            continue
        if alarm.alarm_type == AlarmType.COLLISION:
            if alarm.crane_a_id == crane_id or alarm.crane_b_id == crane_id:
                stats.collision += 1
        else:
            if alarm.crane_a_id == crane_id:
                if alarm.alarm_type == AlarmType.ROTATION_OSCILLATION:
                    stats.rotation_oscillation += 1
                elif alarm.alarm_type == AlarmType.TROLLEY_OVERSPEED:
                    stats.trolley_overspeed += 1
                elif alarm.alarm_type == AlarmType.LOAD_MOMENT_WARNING:
                    stats.load_moment_warning += 1
                elif alarm.alarm_type == AlarmType.WIND_SPEED_WARNING:
                    stats.wind_speed_warning += 1
                elif alarm.alarm_type == AlarmType.WIND_SPEED_SHUTDOWN:
                    stats.wind_speed_shutdown += 1
                elif alarm.alarm_type == AlarmType.ENERGY_QUOTA_WARNING:
                    stats.energy_quota_warning += 1
                elif alarm.alarm_type == AlarmType.ENERGY_QUOTA_EXCEEDED:
                    stats.energy_quota_exceeded += 1
                elif alarm.alarm_type == AlarmType.ENERGY_FORECAST_EXCEEDED:
                    stats.energy_forecast_exceeded += 1
                elif alarm.alarm_type == AlarmType.ENERGY_LIMIT_RECOVERY:
                    stats.energy_limit_recovery += 1

    return stats


def count_freeze_lock(crane_id: str, start_ts: float, end_ts: float) -> FreezeLockStats:
    stats = FreezeLockStats()
    active_freeze: Optional[Dict] = None
    active_lock: Optional[Dict] = None

    for record in freeze_lock_history:
        if record["crane_id"] != crane_id:
            continue
        if not (start_ts <= record["timestamp"] < end_ts):
            continue

        if record["action_type"] == "FREEZE":
            if record["action"] == "START":
                active_freeze = record
                stats.freeze_count += 1
            elif record["action"] == "END" and active_freeze:
                duration = record["timestamp"] - active_freeze["timestamp"]
                stats.freeze_total_seconds += duration
                active_freeze = None

        elif record["action_type"] == "LOCK":
            if record["action"] == "START":
                active_lock = record
                stats.lock_count += 1
            elif record["action"] == "END" and active_lock:
                duration = record["timestamp"] - active_lock["timestamp"]
                stats.lock_total_seconds += duration
                active_lock = None

    if active_freeze:
        end_time = min(end_ts, time.time())
        stats.freeze_total_seconds += end_time - active_freeze["timestamp"]

    if active_lock:
        end_time = min(end_ts, time.time())
        stats.lock_total_seconds += end_time - active_lock["timestamp"]

    return stats


def count_token_usage(crane_id: str, start_ts: float, end_ts: float) -> TokenStats:
    stats = TokenStats()
    queue_times: Dict[str, float] = {}
    wait_durations: List[float] = []

    for event in arb_event_logs:
        if event.crane_id != crane_id:
            continue
        if not (start_ts <= event.timestamp < end_ts):
            continue

        if event.event_type == EventType.TOKEN_ACQUIRED:
            stats.request_count += 1
            req_id = event.details.get("request_id")
            from_queue = event.details.get("from_queue", False)
            if req_id and from_queue and req_id in queue_times:
                wait_duration = event.timestamp - queue_times[req_id]
                wait_durations.append(wait_duration)
                del queue_times[req_id]

        elif event.event_type == EventType.TOKEN_ENQUEUED:
            stats.queue_count += 1
            stats.request_count += 1
            req_id = event.details.get("request_id")
            if req_id:
                queue_times[req_id] = event.timestamp

        elif event.event_type == EventType.TOKEN_DEQUEUED:
            req_id = event.details.get("request_id")
            if req_id and req_id in queue_times:
                del queue_times[req_id]

        elif event.event_type == EventType.TOKEN_REQUEST_TIMEOUT:
            req_id = event.details.get("request_id")
            if req_id and req_id in queue_times:
                del queue_times[req_id]

    if wait_durations:
        stats.avg_wait_seconds = sum(wait_durations) / len(wait_durations)

    return stats


def generate_daily_report_for_crane(crane_id: str, date_str: str) -> Optional[DailyReport]:
    if crane_id not in cranes_config:
        return None

    start_ts, end_ts = get_date_range(date_str)

    report_count, first_ts, last_ts = count_status_reports(crane_id, start_ts, end_ts)
    if report_count == 0:
        return None

    completed_orders = count_completed_orders(crane_id, start_ts, end_ts)
    incomplete_orders = get_incomplete_orders(crane_id, end_ts)
    alarm_stats = count_alarms(crane_id, start_ts, end_ts)
    freeze_lock_stats = count_freeze_lock(crane_id, start_ts, end_ts)
    token_stats = count_token_usage(crane_id, start_ts, end_ts)

    try:
        from maintenance import get_maintenance_daily_stats
        maintenance_stats = get_maintenance_daily_stats(crane_id, start_ts, end_ts)
    except ImportError:
        from models import MaintenanceDailyStats
        maintenance_stats = MaintenanceDailyStats()

    try:
        from inspection import get_inspection_daily_stats, check_overdue_hazards
        check_overdue_hazards()
        inspection_stats = get_inspection_daily_stats(crane_id, start_ts, end_ts)
    except ImportError:
        from models import InspectionDailyStats
        inspection_stats = InspectionDailyStats()

    try:
        from energy_monitor import get_energy_daily_stats
        energy_stats = get_energy_daily_stats(crane_id, start_ts, end_ts)
    except ImportError:
        from models import EnergyDailyStats
        energy_stats = EnergyDailyStats()

    try:
        from emergency_response import get_emergency_daily_stats
        emergency_stats = get_emergency_daily_stats(crane_id, start_ts, end_ts)
    except ImportError:
        emergency_stats = EmergencyDailyStats()

    conflict_detection_stats = count_conflict_detection(start_ts, end_ts)

    work_duration = 0.0
    if first_ts and last_ts:
        work_duration = last_ts - first_ts

    data_status = DailyReportDataStatus.COMPLETE
    remarks = ""
    if incomplete_orders:
        data_status = DailyReportDataStatus.INCOMPLETE
        remarks = f"存在未完成工单: {', '.join(incomplete_orders)}"

    if maintenance_stats.in_maintenance_period:
        if remarks:
            remarks += " | "
        remarks += f"当日处于维保期，维保类型: {maintenance_stats.maintenance_type.value if maintenance_stats.maintenance_type else '未知'}"

    if inspection_stats.inspection_completed:
        if remarks:
            remarks += " | "
        remarks += f"当日已完成巡检，发现隐患{inspection_stats.hazards_found}项"
        if inspection_stats.overdue_hazards > 0:
            remarks += f"，存在超期隐患{inspection_stats.overdue_hazards}项"
    else:
        if remarks:
            remarks += " | "
        remarks += "当日未完成安全巡检"

    if energy_stats.over_limit:
        if remarks:
            remarks += " | "
        remarks += "当日能耗超限"
    elif energy_stats.total_energy_kwh > 0:
        if remarks:
            remarks += " | "
        quota_kwh = 500.0
        try:
            from energy_monitor import cranes_quota_kwh, _default_quota_kwh
            quota_kwh = cranes_quota_kwh.get(crane_id, _default_quota_kwh)
        except ImportError:
            pass
        remarks += f"当日累计能耗{energy_stats.total_energy_kwh:.2f} kWh，配额{quota_kwh:.0f} kWh"

    if energy_stats.forecast_alarm_count > 0 or energy_stats.was_in_limit_list:
        if remarks:
            remarks += " | "
        limit_parts = []
        if energy_stats.forecast_alarm_count > 0:
            limit_parts.append(f"触发能耗预测超标告警{energy_stats.forecast_alarm_count}次")
        if energy_stats.was_in_limit_list:
            limit_parts.append("曾进入限电名单")
        if energy_stats.limit_recovery_count > 0:
            try:
                from energy_monitor import cranes_limit_history
                today_leaves = [e for e in cranes_limit_history
                                if e.get("crane_id") == crane_id and e.get("action") == "LEAVE"
                                and e.get("date") == date_str]
                manual_recoveries = sum(1 for e in today_leaves if e.get("is_manual"))
                auto_recoveries = energy_stats.limit_recovery_count - manual_recoveries
                recovery_desc_parts = []
                if auto_recoveries > 0:
                    recovery_desc_parts.append(f"自动恢复{auto_recoveries}次")
                if manual_recoveries > 0:
                    recovery_desc_parts.append(f"管理员手动解除{manual_recoveries}次")
                if recovery_desc_parts:
                    limit_parts.append("限电恢复(" + "，".join(recovery_desc_parts) + ")")
                else:
                    limit_parts.append(f"限电恢复{energy_stats.limit_recovery_count}次")
            except ImportError:
                limit_parts.append(f"限电恢复{energy_stats.limit_recovery_count}次")
        remarks += "限电情况: " + "，".join(limit_parts)

    if alarm_stats.energy_forecast_exceeded > 0 or alarm_stats.energy_limit_recovery > 0:
        if remarks:
            remarks += " | "
        event_parts = []
        if alarm_stats.energy_forecast_exceeded > 0:
            event_parts.append(f"预测超标事件{alarm_stats.energy_forecast_exceeded}次")
        if alarm_stats.energy_limit_recovery > 0:
            event_parts.append(f"限电恢复事件{alarm_stats.energy_limit_recovery}次")
        remarks += "限电相关事件: " + "，".join(event_parts)

    if emergency_stats.emergency_event_count > 0:
        if remarks:
            remarks += " | "
        level_names = {
            EmergencyLevel.GENERAL: "一般",
            EmergencyLevel.SERIOUS: "严重",
            EmergencyLevel.CRITICAL: "紧急",
        }
        highest_level_str = level_names.get(emergency_stats.highest_emergency_level, "未知") \
            if emergency_stats.highest_emergency_level else "无"
        level_parts = []
        if emergency_stats.general_count > 0:
            level_parts.append(f"一般{emergency_stats.general_count}次")
        if emergency_stats.serious_count > 0:
            level_parts.append(f"严重{emergency_stats.serious_count}次")
        if emergency_stats.critical_count > 0:
            level_parts.append(f"紧急{emergency_stats.critical_count}次")
        remarks += (f"触发应急事件共{emergency_stats.emergency_event_count}次"
                    f"(最高等级: {highest_level_str}, "
                    + "，".join(level_parts) + ")")

    if conflict_detection_stats.detection_count > 0:
        if remarks:
            remarks += " | "
        conflict_parts = [
            f"冲突检测{conflict_detection_stats.detection_count}次",
            f"发现冲突{conflict_detection_stats.total_conflict_count}个",
            f"采纳建议{conflict_detection_stats.accepted_suggestion_count}条",
        ]
        remarks += "跨塔吊作业排班: " + "，".join(conflict_parts)

    report_key = f"{crane_id}_{date_str}"
    existing_report = daily_reports.get(report_key)

    if existing_report and existing_report.status == DailyReportStatus.APPROVED:
        return None

    now = time.time()
    report = DailyReport(
        report_id=str(uuid.uuid4()) if not existing_report else existing_report.report_id,
        crane_id=crane_id,
        report_date=date_str,
        completed_orders=completed_orders,
        total_lifts=report_count,
        work_duration_seconds=work_duration,
        first_report_time=first_ts,
        last_report_time=last_ts,
        alarm_stats=alarm_stats,
        freeze_lock_stats=freeze_lock_stats,
        token_stats=token_stats,
        maintenance_stats=maintenance_stats,
        inspection_stats=inspection_stats,
        energy_stats=energy_stats,
        emergency_stats=emergency_stats,
        conflict_detection_stats=conflict_detection_stats,
        data_status=data_status,
        incomplete_orders=incomplete_orders,
        remarks=remarks,
        status=DailyReportStatus.PENDING,
        generated_at=existing_report.generated_at if existing_report else now,
        updated_at=now,
    )

    daily_reports[report_key] = report
    return report


def generate_daily_reports(date_str: Optional[str] = None, crane_id: Optional[str] = None) -> Dict:
    target_date = date_str or get_today_date_str()
    generated = []
    skipped = []
    locked = []

    crane_ids = [crane_id] if crane_id else list(cranes_config.keys())

    for cid in crane_ids:
        report_key = f"{cid}_{target_date}"
        existing_report = daily_reports.get(report_key)

        if existing_report and existing_report.status == DailyReportStatus.APPROVED:
            locked.append(cid)
            continue

        report = generate_daily_report_for_crane(cid, target_date)
        if report is None:
            skipped.append(cid)
        else:
            generated.append(report)

    return {
        "date": target_date,
        "generated_count": len(generated),
        "skipped_count": len(skipped),
        "locked_count": len(locked),
        "generated_reports": generated,
        "skipped_cranes": skipped,
        "locked_cranes": locked,
    }


def get_daily_reports(start_date: Optional[str] = None, end_date: Optional[str] = None,
                      status: Optional[DailyReportStatus] = None,
                      crane_id: Optional[str] = None) -> List[DailyReport]:
    reports = list(daily_reports.values())

    if crane_id:
        reports = [r for r in reports if r.crane_id == crane_id]

    if status:
        reports = [r for r in reports if r.status == status]

    if start_date:
        reports = [r for r in reports if r.report_date >= start_date]

    if end_date:
        reports = [r for r in reports if r.report_date <= end_date]

    reports.sort(key=lambda r: (r.report_date, r.crane_id), reverse=True)
    return reports


def get_daily_report(report_id: str) -> Optional[DailyReport]:
    for report in daily_reports.values():
        if report.report_id == report_id:
            return report
    return None


def approve_daily_report(report_id: str, action: str, approver: str,
                         remarks: Optional[str] = None) -> Dict:
    report = get_daily_report(report_id)
    if not report:
        return {"error": "日报不存在"}

    if report.status == DailyReportStatus.APPROVED:
        return {"error": "日报已审批通过，不可重复审批"}

    if action not in ["APPROVE", "REJECT"]:
        return {"error": "无效的审批动作，必须是 APPROVE 或 REJECT"}

    now = time.time()
    if action == "APPROVE":
        report.status = DailyReportStatus.APPROVED
    else:
        report.status = DailyReportStatus.REJECTED

    report.approver = approver
    report.approval_remarks = remarks or ""
    report.approved_at = now
    report.updated_at = now

    return {"success": True, "report": report}


def generate_summary(start_date: str, end_date: str) -> DailyReportSummaryResponse:
    reports = get_daily_reports(start_date=start_date, end_date=end_date)

    crane_stats: Dict[str, Dict] = {}
    for report in reports:
        if report.crane_id not in crane_stats:
            config = cranes_config.get(report.crane_id)
            crane_stats[report.crane_id] = {
                "crane_name": config.name if config else report.crane_id,
                "total_reports": 0,
                "total_completed_orders": 0,
                "total_lifts": 0,
                "total_work_seconds": 0.0,
                "total_alarms": 0,
                "total_freezes": 0,
                "total_locks": 0,
                "total_token_requests": 0,
                "total_token_queues": 0,
            }

        s = crane_stats[report.crane_id]
        s["total_reports"] += 1
        s["total_completed_orders"] += report.completed_orders
        s["total_lifts"] += report.total_lifts
        s["total_work_seconds"] += report.work_duration_seconds
        s["total_alarms"] += (
            report.alarm_stats.collision +
            report.alarm_stats.rotation_oscillation +
            report.alarm_stats.trolley_overspeed +
            report.alarm_stats.load_moment_warning +
            report.alarm_stats.wind_speed_warning +
            report.alarm_stats.wind_speed_shutdown +
            report.alarm_stats.energy_quota_warning +
            report.alarm_stats.energy_quota_exceeded +
            report.alarm_stats.energy_forecast_exceeded +
            report.alarm_stats.energy_limit_recovery
        )
        s["total_freezes"] += report.freeze_lock_stats.freeze_count
        s["total_locks"] += report.freeze_lock_stats.lock_count
        s["total_token_requests"] += report.token_stats.request_count
        s["total_token_queues"] += report.token_stats.queue_count

    summaries = []
    for crane_id, stats in crane_stats.items():
        summaries.append(DailyReportSummaryItem(
            crane_id=crane_id,
            crane_name=stats["crane_name"],
            total_reports=stats["total_reports"],
            total_completed_orders=stats["total_completed_orders"],
            total_lifts=stats["total_lifts"],
            total_work_seconds=stats["total_work_seconds"],
            total_alarms=stats["total_alarms"],
            total_freezes=stats["total_freezes"],
            total_locks=stats["total_locks"],
            total_token_requests=stats["total_token_requests"],
            total_token_queues=stats["total_token_queues"],
        ))

    summaries.sort(key=lambda x: x.crane_id)

    return DailyReportSummaryResponse(
        start_date=start_date,
        end_date=end_date,
        summaries=summaries,
    )
