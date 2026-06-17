import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from models import (
    MaintenanceWindow,
    MaintenanceWindowCreate,
    MaintenanceRecord,
    MaintenanceConfirmRequest,
    MaintenanceStatus,
    MaintenanceType,
    MaintenanceAlarmType,
    MaintenanceAlarmEvent,
    CraneMaintenanceStatus,
    MaintenanceDailyStats,
    MaintenanceWindowQuery,
)
from collision import cranes_config

DEFAULT_MAINTENANCE_CYCLE_DAYS = 30
MAINTENANCE_DUE_SOON_DAYS = 7

maintenance_windows: Dict[str, MaintenanceWindow] = {}
maintenance_records: Dict[str, MaintenanceRecord] = {}
maintenance_alarms: List[MaintenanceAlarmEvent] = []
crane_last_maintenance: Dict[str, float] = {}
crane_maintenance_cycle: Dict[str, int] = {}
window_id_index: Dict[str, str] = {}
crane_suppressed_alarm_counts: Dict[str, Dict[str, int]] = {}


def init_maintenance_module():
    for crane_id in cranes_config.keys():
        if crane_id not in crane_maintenance_cycle:
            crane_maintenance_cycle[crane_id] = DEFAULT_MAINTENANCE_CYCLE_DAYS
        if crane_id not in crane_suppressed_alarm_counts:
            crane_suppressed_alarm_counts[crane_id] = {
                "collision": 0,
                "anomaly": 0,
            }


def _generate_window_id() -> str:
    return f"MW-{uuid.uuid4().hex[:8].upper()}"


def _generate_record_id() -> str:
    return f"MR-{uuid.uuid4().hex[:8].upper()}"


def _generate_alarm_id() -> str:
    return f"MA-{uuid.uuid4().hex[:8].upper()}"


def _log_maintenance_alarm(
    alarm_type: MaintenanceAlarmType,
    window_id: str,
    crane_id: str,
    message: str,
    details: Optional[Dict] = None,
):
    now = time.time()
    alarm = MaintenanceAlarmEvent(
        alarm_id=_generate_alarm_id(),
        alarm_type=alarm_type,
        window_id=window_id,
        crane_id=crane_id,
        timestamp=now,
        datetime_str=datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        message=message,
        details=details or {},
    )
    maintenance_alarms.append(alarm)
    return alarm


def calculate_next_due_date(crane_id: str, from_time: Optional[float] = None) -> Optional[float]:
    last_time = from_time or crane_last_maintenance.get(crane_id)
    if last_time is None:
        return None
    cycle_days = crane_maintenance_cycle.get(crane_id, DEFAULT_MAINTENANCE_CYCLE_DAYS)
    return last_time + cycle_days * 86400


def get_days_until_due(crane_id: str) -> Optional[int]:
    next_due = calculate_next_due_date(crane_id)
    if next_due is None:
        return None
    now = time.time()
    diff_seconds = next_due - now
    return max(0, int(diff_seconds // 86400))


def get_crane_current_window(crane_id: str) -> Optional[MaintenanceWindow]:
    now = time.time()
    for window in maintenance_windows.values():
        if window.crane_id != crane_id:
            continue
        if window.status in (MaintenanceStatus.COMPLETED,):
            continue
        if window.start_time <= now <= window.end_time:
            return window
        if window.status in (MaintenanceStatus.PENDING, MaintenanceStatus.IN_PROGRESS, MaintenanceStatus.ABNORMAL):
            return window
    return None


def is_crane_in_maintenance(crane_id: str) -> bool:
    window = get_crane_current_window(crane_id)
    if window is None:
        return False
    now = time.time()
    return window.start_time <= now <= window.end_time and window.is_active


def increment_suppressed_alarm(crane_id: str, alarm_category: str):
    if crane_id not in crane_suppressed_alarm_counts:
        crane_suppressed_alarm_counts[crane_id] = {"collision": 0, "anomaly": 0}
    if alarm_category in crane_suppressed_alarm_counts[crane_id]:
        crane_suppressed_alarm_counts[crane_id][alarm_category] += 1


def get_suppressed_alarms_for_period(crane_id: str, start_ts: float, end_ts: float) -> Tuple[int, int]:
    collision_count = 0
    anomaly_count = 0
    for window in maintenance_windows.values():
        if window.crane_id != crane_id:
            continue
        if not window.is_active:
            continue
        overlap_start = max(window.start_time, start_ts)
        overlap_end = min(window.end_time, end_ts)
        if overlap_start < overlap_end:
            collision_count += crane_suppressed_alarm_counts.get(crane_id, {}).get("collision", 0)
            anomaly_count += crane_suppressed_alarm_counts.get(crane_id, {}).get("anomaly", 0)
    return collision_count, anomaly_count


def _release_crane_tokens_on_maintenance(crane_id: str) -> int:
    try:
        from arbiter import (
            cranes_held_tokens,
            cranes_pending_requests,
            token_statuses,
            log_arb_event,
            EventType,
            _try_grant_token_to_next,
        )
        released_count = 0

        held = cranes_held_tokens.get(crane_id, set()).copy()
        for sector_id in held:
            ts = token_statuses.get(sector_id)
            if ts and ts.holder_crane_id == crane_id:
                log_arb_event(EventType.TOKEN_REVOKED, crane_id=crane_id, sector_id=sector_id, details={
                    "reason": "maintenance_shutdown",
                    "acquired_at": ts.acquired_at,
                })
                ts.holder_crane_id = None
                ts.acquired_at = None
                ts.expires_at = None
                cranes_held_tokens[crane_id].discard(sector_id)
                _try_grant_token_to_next(sector_id)
                released_count += 1

        pending = cranes_pending_requests.get(crane_id, {}).copy()
        for sector_id, req_id in pending.items():
            ts = token_statuses.get(sector_id)
            if ts:
                ts.wait_queue = [i for i in ts.wait_queue if i.request_id != req_id]
            cranes_pending_requests[crane_id].pop(sector_id, None)
            log_arb_event(EventType.TOKEN_DEQUEUED, crane_id=crane_id, sector_id=sector_id, details={
                "request_id": req_id,
                "reason": "maintenance_shutdown",
            })

        return released_count
    except ImportError:
        return 0


def create_maintenance_window(create: MaintenanceWindowCreate) -> Dict:
    if create.crane_id not in cranes_config:
        return {"error": f"塔吊 {create.crane_id} 不存在"}

    if create.start_time >= create.end_time:
        return {"error": "停机窗口开始时间必须早于结束时间"}

    if create.start_time < time.time() - 3600:
        return {"error": "停机窗口开始时间不能早于1小时前"}

    existing_current = get_crane_current_window(create.crane_id)
    if existing_current:
        return {
            "error": f"塔吊 {create.crane_id} 当前存在未完成的维保窗口 {existing_current.window_id}",
            "existing_window": existing_current,
        }

    for window in maintenance_windows.values():
        if window.crane_id != create.crane_id:
            continue
        if window.status in (MaintenanceStatus.COMPLETED,):
            continue
        if not (create.end_time < window.start_time or create.start_time > window.end_time):
            return {
                "error": f"与现有维保窗口 {window.window_id} 时间重叠",
                "conflicting_window": window,
            }

    now = time.time()
    window_id = _generate_window_id()
    window = MaintenanceWindow(
        window_id=window_id,
        crane_id=create.crane_id,
        start_time=create.start_time,
        end_time=create.end_time,
        maintenance_type=create.maintenance_type,
        responsible_person=create.responsible_person,
        remarks=create.remarks or "",
        status=MaintenanceStatus.PENDING,
        created_at=now,
        updated_at=now,
        is_active=False,
        is_timeout=False,
    )
    maintenance_windows[window_id] = window
    window_id_index[window_id] = window_id

    return {
        "success": True,
        "window": window,
        "message": f"维保停机窗口创建成功，自动进入待维保状态",
    }


def check_and_transition_window_status(window_id: str) -> MaintenanceWindow:
    window = maintenance_windows.get(window_id)
    if not window:
        return None

    now = time.time()
    updated = False

    if window.status == MaintenanceStatus.PENDING and now >= window.start_time:
        window.status = MaintenanceStatus.IN_PROGRESS
        window.started_at = now
        window.is_active = True
        window.updated_at = now
        updated = True
        released = _release_crane_tokens_on_maintenance(window.crane_id)
        print(f"[维保] 塔吊 {window.crane_id} 进入维保中状态，释放令牌 {released} 个")

    if window.status == MaintenanceStatus.IN_PROGRESS and now > window.end_time:
        window.status = MaintenanceStatus.ABNORMAL
        window.is_timeout = True
        window.is_active = False
        window.updated_at = now
        updated = True
        _log_maintenance_alarm(
            MaintenanceAlarmType.MAINTENANCE_TIMEOUT,
            window_id,
            window.crane_id,
            f"维保停机窗口已结束但维保未确认完成，状态标记为异常，窗口ID: {window_id}",
            {
                "window_start": window.start_time,
                "window_end": window.end_time,
                "responsible_person": window.responsible_person,
            },
        )
        print(f"[维保告警] 塔吊 {window.crane_id} 维保超时，状态已标记为异常")

    if window.status == MaintenanceStatus.PENDING:
        next_due = calculate_next_due_date(window.crane_id)
        if next_due and now >= next_due:
            _log_maintenance_alarm(
                MaintenanceAlarmType.MAINTENANCE_OVERDUE,
                window_id,
                window.crane_id,
                f"塔吊已超过维保截止日期，维保截止: {datetime.fromtimestamp(next_due).strftime('%Y-%m-%d %H:%M:%S')}",
                {"next_due_date": next_due},
            )

    if updated:
        maintenance_windows[window_id] = window

    return window


def check_all_windows() -> List[MaintenanceWindow]:
    updated_windows = []
    for window_id in list(maintenance_windows.keys()):
        original_status = maintenance_windows[window_id].status
        original_active = maintenance_windows[window_id].is_active
        updated = check_and_transition_window_status(window_id)
        if updated and (updated.status != original_status or updated.is_active != original_active):
            updated_windows.append(updated)
    return updated_windows


def confirm_maintenance_complete(window_id: str, confirm: MaintenanceConfirmRequest) -> Dict:
    window = maintenance_windows.get(window_id)
    if not window:
        return {"error": f"维保窗口 {window_id} 不存在"}

    if window.status == MaintenanceStatus.COMPLETED:
        return {"error": "该维保窗口已确认完成，不可重复确认"}

    now = time.time()

    if not confirm.inspection_results or not confirm.inspection_results.strip():
        return {"error": "检测结果描述不能为空"}

    if not confirm.confirmed_by or not confirm.confirmed_by.strip():
        return {"error": "确认人不能为空"}

    record_id = _generate_record_id()
    next_suggested = confirm.next_suggested_maintenance_date
    if next_suggested is None:
        cycle_days = crane_maintenance_cycle.get(window.crane_id, DEFAULT_MAINTENANCE_CYCLE_DAYS)
        next_suggested = now + cycle_days * 86400

    record = MaintenanceRecord(
        record_id=record_id,
        window_id=window_id,
        crane_id=window.crane_id,
        replaced_parts=confirm.replaced_parts or [],
        inspection_results=confirm.inspection_results,
        next_suggested_maintenance_date=next_suggested,
        confirmed_by=confirm.confirmed_by,
        confirmed_at=now,
        remarks=confirm.remarks or "",
    )
    maintenance_records[record_id] = record

    window.status = MaintenanceStatus.COMPLETED
    window.completed_at = now
    window.is_active = False
    window.updated_at = now
    maintenance_windows[window_id] = window

    crane_last_maintenance[window.crane_id] = next_suggested
    crane_suppressed_alarm_counts[window.crane_id] = {"collision": 0, "anomaly": 0}

    return {
        "success": True,
        "window": window,
        "record": record,
        "message": f"维保已确认完成，下次建议维保时间: {datetime.fromtimestamp(next_suggested).strftime('%Y-%m-%d %H:%M:%S')}",
    }


def get_maintenance_windows(query: MaintenanceWindowQuery) -> List[MaintenanceWindow]:
    results = list(maintenance_windows.values())

    if query.crane_id:
        results = [w for w in results if w.crane_id == query.crane_id]
    if query.status:
        results = [w for w in results if w.status == query.status]
    if query.maintenance_type:
        results = [w for w in results if w.maintenance_type == query.maintenance_type]
    if query.start_from:
        results = [w for w in results if w.start_time >= query.start_from]
    if query.end_before:
        results = [w for w in results if w.end_time <= query.end_before]

    results.sort(key=lambda w: w.start_time, reverse=True)
    return results


def get_maintenance_window(window_id: str) -> Optional[MaintenanceWindow]:
    return maintenance_windows.get(window_id)


def get_maintenance_records_by_window(window_id: str) -> List[MaintenanceRecord]:
    return [r for r in maintenance_records.values() if r.window_id == window_id]


def get_maintenance_history(crane_id: str) -> List[Dict]:
    windows = [w for w in maintenance_windows.values() if w.crane_id == crane_id]
    windows.sort(key=lambda w: w.created_at, reverse=True)

    history = []
    for window in windows:
        records = get_maintenance_records_by_window(window.window_id)
        history.append({
            "window": window,
            "records": records,
        })
    return history


def get_crane_maintenance_status(crane_id: str) -> Optional[CraneMaintenanceStatus]:
    if crane_id not in cranes_config:
        return None

    config = cranes_config[crane_id]
    last_time = crane_last_maintenance.get(crane_id)
    next_due = calculate_next_due_date(crane_id)
    days_until = get_days_until_due(crane_id)
    current_window = get_crane_current_window(crane_id)

    if current_window:
        m_status = current_window.status
    elif next_due is not None and time.time() > next_due:
        m_status = MaintenanceStatus.ABNORMAL
    elif last_time is None:
        m_status = MaintenanceStatus.PENDING
    else:
        m_status = MaintenanceStatus.COMPLETED

    history_count = len([w for w in maintenance_windows.values()
                         if w.crane_id == crane_id and w.status == MaintenanceStatus.COMPLETED])

    return CraneMaintenanceStatus(
        crane_id=crane_id,
        crane_name=config.name,
        last_maintenance_time=last_time,
        next_due_date=next_due,
        days_until_due=days_until,
        current_window=current_window,
        maintenance_status=m_status,
        maintenance_history_count=history_count,
        cycle_days=crane_maintenance_cycle.get(crane_id, DEFAULT_MAINTENANCE_CYCLE_DAYS),
    )


def get_all_cranes_maintenance_status() -> List[CraneMaintenanceStatus]:
    statuses = []
    for crane_id in cranes_config.keys():
        s = get_crane_maintenance_status(crane_id)
        if s:
            statuses.append(s)
    return statuses


def get_active_maintenance_windows() -> List[MaintenanceWindow]:
    now = time.time()
    active = []
    for window in maintenance_windows.values():
        if window.is_active or (window.start_time <= now <= window.end_time and
                                window.status in (MaintenanceStatus.PENDING, MaintenanceStatus.IN_PROGRESS)):
            active.append(window)
    return active


def get_maintenance_alarms(
    crane_id: Optional[str] = None,
    alarm_type: Optional[MaintenanceAlarmType] = None,
    limit: int = 100,
) -> List[MaintenanceAlarmEvent]:
    results = maintenance_alarms
    if crane_id:
        results = [a for a in results if a.crane_id == crane_id]
    if alarm_type:
        results = [a for a in results if a.alarm_type == alarm_type]
    return results[-limit:]


def get_maintenance_daily_stats(crane_id: str, start_ts: float, end_ts: float) -> MaintenanceDailyStats:
    stats = MaintenanceDailyStats()

    for window in maintenance_windows.values():
        if window.crane_id != crane_id:
            continue
        overlap_start = max(window.start_time, start_ts)
        overlap_end = min(window.end_time, end_ts)
        if overlap_start < overlap_end:
            stats.in_maintenance_period = True
            stats.maintenance_window_id = window.window_id
            stats.maintenance_type = window.maintenance_type
            break

    collision_suppressed, anomaly_suppressed = get_suppressed_alarms_for_period(crane_id, start_ts, end_ts)
    stats.suppressed_collision_alarms = collision_suppressed
    stats.supplied_anomaly_alarms = anomaly_suppressed
    stats.total_suppressed_alarms = collision_suppressed + anomaly_suppressed

    return stats


def set_crane_maintenance_cycle(crane_id: str, cycle_days: int) -> Dict:
    if crane_id not in cranes_config:
        return {"error": f"塔吊 {crane_id} 不存在"}
    if cycle_days < 1 or cycle_days > 365:
        return {"error": "维保周期必须在1-365天之间"}
    crane_maintenance_cycle[crane_id] = cycle_days
    return {
        "success": True,
        "crane_id": crane_id,
        "cycle_days": cycle_days,
    }


def check_due_soon_alarms():
    now = time.time()
    for crane_id in cranes_config.keys():
        next_due = calculate_next_due_date(crane_id)
        if next_due is None:
            continue
        days_left = (next_due - now) / 86400
        if 0 < days_left <= MAINTENANCE_DUE_SOON_DAYS:
            current_window = get_crane_current_window(crane_id)
            if current_window is None:
                recent_alarms = [a for a in maintenance_alarms
                                 if a.crane_id == crane_id
                                 and a.alarm_type == MaintenanceAlarmType.MAINTENANCE_DUE_SOON
                                 and now - a.timestamp < 86400]
                if not recent_alarms:
                    _log_maintenance_alarm(
                        MaintenanceAlarmType.MAINTENANCE_DUE_SOON,
                        "",
                        crane_id,
                        f"塔吊维保即将到期，剩余 {int(days_left)} 天，下次维保截止: {datetime.fromtimestamp(next_due).strftime('%Y-%m-%d %H:%M:%S')}",
                        {
                            "next_due_date": next_due,
                            "days_left": int(days_left),
                        },
                    )
