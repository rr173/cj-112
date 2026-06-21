import time
import threading
from datetime import datetime, timedelta

from fastapi import FastAPI

from models import TowerCraneConfig, LockStatus
from collision import (
    cranes_config,
    cranes_lock_status,
    pair_safety_thresholds,
    DEFAULT_SAFETY_THRESHOLD,
    get_pair_key,
)
from arbiter import (
    cranes_held_tokens,
    cranes_pending_requests,
    token_statuses,
    clean_expired_tokens_and_waiters,
    rebuild_all_overlap_sectors,
)
from routes_crane import router as crane_router
from routes_arb import router as arb_router
from routes_order import router as order_router
from routes_anomaly import router as anomaly_router
from routes_report import router as report_router
from routes_maintenance import router as maintenance_router
from routes_operator import router as operator_router
from routes_path import router as path_router
from routes_inspection import router as inspection_router
from routes_cooperative import router as cooperative_router
from routes_load_moment import router as load_moment_router
from routes_wind_speed import router as wind_speed_router
from routes_energy import router as energy_router
from routes_emergency import router as emergency_router
from routes_conflict import router as conflict_router
from routes_fatigue import router as fatigue_router
from routes_permit import router as permit_router
from anomaly_detector import init_anomaly_detector
from daily_report import init_daily_report_module, generate_daily_reports, get_today_date_str
from maintenance import init_maintenance_module, check_all_windows, check_due_soon_alarms
from operator_training import init_operator_module
from path_planner import init_path_planner
from inspection import init_inspection_module, check_overdue_hazards
from load_moment_monitor import init_load_moment_monitor_module
from wind_speed_monitor import init_wind_speed_monitor_module
from energy_monitor import init_energy_monitor_module, check_and_reset_daily
from emergency_response import init_emergency_response_module, check_and_trigger_emergency, check_auto_escalation
from conflict_scheduler import init_conflict_scheduler_module
from work_permit import init_work_permit_module, check_and_revoke_if_needed

app = FastAPI(title="塔吊防碰撞联锁服务", description="建筑工地多塔吊防碰撞实时监测系统")

app.include_router(crane_router)
app.include_router(arb_router)
app.include_router(order_router)
app.include_router(anomaly_router)
app.include_router(report_router)
app.include_router(maintenance_router)
app.include_router(operator_router)
app.include_router(path_router)
app.include_router(inspection_router)
app.include_router(cooperative_router)
app.include_router(load_moment_router)
app.include_router(wind_speed_router)
app.include_router(energy_router)
app.include_router(emergency_router)
app.include_router(conflict_router)
app.include_router(fatigue_router)
app.include_router(permit_router)


_daily_report_scheduler_thread: threading.Thread = None
_maintenance_scheduler_thread: threading.Thread = None
_inspection_scheduler_thread: threading.Thread = None
_energy_reset_scheduler_thread: threading.Thread = None
_emergency_scheduler_thread: threading.Thread = None
_work_permit_scheduler_thread: threading.Thread = None
_scheduler_running = False


def _get_next_run_time() -> float:
    now = datetime.now()
    next_run = now.replace(hour=23, minute=50, second=0, microsecond=0)
    if now >= next_run:
        next_run = next_run + timedelta(days=1)
    return (next_run - now).total_seconds()


def _daily_report_scheduler_loop():
    global _scheduler_running
    _scheduler_running = True
    while _scheduler_running:
        wait_seconds = _get_next_run_time()
        print(f"[日报定时任务] 下一次自动生成日报将在 {wait_seconds:.0f} 秒后执行 ({datetime.now() + timedelta(seconds=wait_seconds):%Y-%m-%d %H:%M:%S})")
        time.sleep(min(wait_seconds, 60))
        if not _scheduler_running:
            break
        if datetime.now() >= datetime.now().replace(hour=23, minute=50, second=0, microsecond=0):
            try:
                today = get_today_date_str()
                print(f"[日报定时任务] 开始自动生成 {today} 的日报...")
                result = generate_daily_reports(today)
                print(f"[日报定时任务] 日报生成完成: 成功 {result['generated_count']} 份, 跳过 {result['skipped_count']} 份, 锁定 {result['locked_count']} 份")
            except Exception as e:
                print(f"[日报定时任务] 自动生成日报失败: {e}")
            time.sleep(60)


def _maintenance_scheduler_loop():
    global _scheduler_running
    _scheduler_running = True
    last_due_check = 0.0
    while _scheduler_running:
        try:
            updated = check_all_windows()
            if updated:
                for w in updated:
                    print(f"[维保定时任务] 塔吊 {w.crane_id} 维保窗口状态变更: {w.status.value}, 窗口ID: {w.window_id}")

            now = time.time()
            if now - last_due_check >= 3600:
                check_due_soon_alarms()
                last_due_check = now
        except Exception as e:
            print(f"[维保定时任务] 执行异常: {e}")

        time.sleep(30)


def _inspection_scheduler_loop():
    global _scheduler_running
    _scheduler_running = True
    while _scheduler_running:
        try:
            overdue = check_overdue_hazards()
            if overdue:
                for h in overdue:
                    print(f"[巡检定时任务] 隐患超期告警: 塔吊 {h.crane_id} - {h.item_name}, 隐患ID: {h.hazard_id}, 责任人: {h.responsible_person}, 已超期 {round((time.time() - h.deadline)/3600, 1)} 小时")
        except Exception as e:
            print(f"[巡检定时任务] 超期检查异常: {e}")

        time.sleep(300)


def _get_seconds_to_midnight() -> float:
    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds()


def _energy_reset_scheduler_loop():
    global _scheduler_running
    _scheduler_running = True
    while _scheduler_running:
        wait_seconds = _get_seconds_to_midnight()
        print(f"[能耗零点重置] 距离下次零点重置还有 {wait_seconds:.0f} 秒")
        time.sleep(min(wait_seconds, 60))
        if not _scheduler_running:
            break
        now = datetime.now()
        if now.hour == 0 and now.minute == 0:
            try:
                check_and_reset_daily()
                print(f"[能耗零点重置] 零点重置完成")
            except Exception as e:
                print(f"[能耗零点重置] 零点重置失败: {e}")
            time.sleep(65)


def _emergency_scheduler_loop():
    global _scheduler_running
    _scheduler_running = True
    while _scheduler_running:
        try:
            triggered = check_and_trigger_emergency()
            if triggered:
                for event in triggered:
                    print(f"[应急响应定时检查] 触发应急事件: {event.event_id}, "
                          f"等级: {event.emergency_level.value}, 规则: {event.rule_name}")
        except Exception as e:
            print(f"[应急响应定时检查] 执行异常: {e}")
        try:
            escalated = check_auto_escalation()
            if escalated:
                for event in escalated:
                    print(f"[应急响应自动提级] 事件 {event.event_id} 自动提级到 "
                          f"{event.emergency_level.value}")
        except Exception as e:
            print(f"[应急响应自动提级] 执行异常: {e}")
        time.sleep(10)


def _work_permit_scheduler_loop():
    global _scheduler_running
    _scheduler_running = True
    while _scheduler_running:
        try:
            revoked_count = 0
            for cid in list(cranes_config.keys()):
                revoked = check_and_revoke_if_needed(cid)
                if revoked:
                    revoked_count += 1
                    print(f"[作业许可定时检查] 塔吊 {cid} 许可证已被自动吊销")
            if revoked_count > 0:
                print(f"[作业许可定时检查] 本次检查共吊销 {revoked_count} 个作业许可证")
        except Exception as e:
            print(f"[作业许可定时检查] 执行异常: {e}")
        time.sleep(30)


@app.on_event("startup")
def init_cranes():
    preset_cranes = [
        TowerCraneConfig(
            crane_id="CRANE-001",
            name="1号塔吊",
            tower_x=0.0,
            tower_y=0.0,
            tower_z=50.0,
            arm_length=60.0,
            max_load=10.0,
            min_angle=0.0,
            max_angle=360.0,
        ),
        TowerCraneConfig(
            crane_id="CRANE-002",
            name="2号塔吊",
            tower_x=40.0,
            tower_y=30.0,
            tower_z=55.0,
            arm_length=55.0,
            max_load=8.0,
            min_angle=0.0,
            max_angle=360.0,
        ),
        TowerCraneConfig(
            crane_id="CRANE-003",
            name="3号塔吊",
            tower_x=-35.0,
            tower_y=45.0,
            tower_z=48.0,
            arm_length=50.0,
            max_load=12.0,
            min_angle=45.0,
            max_angle=315.0,
        ),
    ]
    for c in preset_cranes:
        cranes_config[c.crane_id] = c
        cranes_lock_status[c.crane_id] = LockStatus(
            crane_id=c.crane_id, is_locked=False
        )
        cranes_held_tokens[c.crane_id] = set()
        cranes_pending_requests[c.crane_id] = {}

    crane_ids = list(cranes_config.keys())
    for i in range(len(crane_ids)):
        for j in range(i + 1, len(crane_ids)):
            pair_safety_thresholds[get_pair_key(crane_ids[i], crane_ids[j])] = DEFAULT_SAFETY_THRESHOLD

    rebuild_all_overlap_sectors()
    init_anomaly_detector()
    init_daily_report_module()
    init_maintenance_module()
    init_operator_module()
    init_path_planner()
    init_inspection_module()
    from cooperative_lift import init_cooperative_lift_module
    init_cooperative_lift_module()
    init_load_moment_monitor_module()
    init_wind_speed_monitor_module()
    init_energy_monitor_module()
    init_emergency_response_module()
    init_conflict_scheduler_module()
    from fatigue_monitor import init_fatigue_monitor_module
    init_fatigue_monitor_module()
    init_work_permit_module()

    global _daily_report_scheduler_thread
    if _daily_report_scheduler_thread is None or not _daily_report_scheduler_thread.is_alive():
        _daily_report_scheduler_thread = threading.Thread(target=_daily_report_scheduler_loop, daemon=True)
        _daily_report_scheduler_thread.start()
        print("[日报定时任务] 已启动，每天 23:50 自动生成当日日报")

    global _maintenance_scheduler_thread
    if _maintenance_scheduler_thread is None or not _maintenance_scheduler_thread.is_alive():
        _maintenance_scheduler_thread = threading.Thread(target=_maintenance_scheduler_loop, daemon=True)
        _maintenance_scheduler_thread.start()
        print("[维保定时任务] 已启动，每30秒检查维保窗口状态，每小时检查维保到期提醒")

    global _inspection_scheduler_thread
    if _inspection_scheduler_thread is None or not _inspection_scheduler_thread.is_alive():
        _inspection_scheduler_thread = threading.Thread(target=_inspection_scheduler_loop, daemon=True)
        _inspection_scheduler_thread.start()
        print("[巡检定时任务] 已启动，每5分钟检查一次隐患超期情况")

    global _energy_reset_scheduler_thread
    if _energy_reset_scheduler_thread is None or not _energy_reset_scheduler_thread.is_alive():
        _energy_reset_scheduler_thread = threading.Thread(target=_energy_reset_scheduler_loop, daemon=True)
        _energy_reset_scheduler_thread.start()
        print("[能耗零点重置] 已启动，每天零点自动重置所有塔吊当日能耗数据")

    global _emergency_scheduler_thread
    if _emergency_scheduler_thread is None or not _emergency_scheduler_thread.is_alive():
        _emergency_scheduler_thread = threading.Thread(target=_emergency_scheduler_loop, daemon=True)
        _emergency_scheduler_thread.start()
        print("[应急响应定时检查] 已启动，每10秒检查一次复合告警")

    global _work_permit_scheduler_thread
    if _work_permit_scheduler_thread is None or not _work_permit_scheduler_thread.is_alive():
        _work_permit_scheduler_thread = threading.Thread(target=_work_permit_scheduler_loop, daemon=True)
        _work_permit_scheduler_thread.start()
        print("[作业许可定时检查] 已启动，每30秒检查一次许可证条件变化")


@app.on_event("shutdown")
def shutdown_event():
    global _scheduler_running
    _scheduler_running = False
    print("[定时任务] 正在停止所有定时任务...")


@app.get("/health", summary="健康检查")
def health_check():
    clean_expired_tokens_and_waiters()
    from collision import cranes_current_status, alarm_history, cranes_lock_status
    from arbiter import overlap_sectors, token_statuses
    from anomaly_detector import (
        cranes_sliding_window,
        cranes_anomaly_events,
        cranes_freeze_status,
        refresh_all_freeze_status,
    )
    from daily_report import daily_reports, status_report_history, DailyReportStatus
    from maintenance import (
        maintenance_windows,
        maintenance_alarms,
        MaintenanceStatus,
        check_all_windows,
    )
    from operator_training import (
        operators as _operators,
        assessment_records as _assessment_records,
        crane_operator_bindings as _crane_operator_bindings,
        shift_handover_records as _shift_handover_records,
    )
    from inspection import (
        inspection_reports as _inspection_reports,
        hazards as _hazards,
        HazardStatus as _HazardStatus,
    )
    from emergency_response import (
        composite_alarm_rules,
        emergency_events,
    )
    from models import EmergencyLevel, EmergencyEventStatus
    refresh_all_freeze_status()
    check_all_windows()
    check_overdue_hazards()
    total_anomaly_events = sum(len(events) for events in cranes_anomaly_events.values())
    frozen_cranes = sum(1 for f in cranes_freeze_status.values() if f.is_frozen)
    total_window_records = sum(len(w) for w in cranes_sliding_window.values())

    from models import AlarmType
    rotation_alarms = sum(1 for a in alarm_history if a.alarm_type == AlarmType.ROTATION_OSCILLATION)
    overspeed_alarms = sum(1 for a in alarm_history if a.alarm_type == AlarmType.TROLLEY_OVERSPEED)
    moment_alarms = sum(1 for a in alarm_history if a.alarm_type == AlarmType.LOAD_MOMENT_WARNING)
    collision_alarms = sum(1 for a in alarm_history if a.alarm_type == AlarmType.COLLISION)
    wind_warning_alarms = sum(1 for a in alarm_history if a.alarm_type == AlarmType.WIND_SPEED_WARNING)
    wind_shutdown_alarms = sum(1 for a in alarm_history if a.alarm_type == AlarmType.WIND_SPEED_SHUTDOWN)

    total_reports = len(daily_reports)
    pending_reports = sum(1 for r in daily_reports.values() if r.status == DailyReportStatus.PENDING)
    approved_reports = sum(1 for r in daily_reports.values() if r.status == DailyReportStatus.APPROVED)
    rejected_reports = sum(1 for r in daily_reports.values() if r.status == DailyReportStatus.REJECTED)
    total_status_records = sum(len(records) for records in status_report_history.values())

    total_m_windows = len(maintenance_windows)
    pending_m = sum(1 for w in maintenance_windows.values() if w.status == MaintenanceStatus.PENDING)
    in_progress_m = sum(1 for w in maintenance_windows.values() if w.status == MaintenanceStatus.IN_PROGRESS)
    completed_m = sum(1 for w in maintenance_windows.values() if w.status == MaintenanceStatus.COMPLETED)
    abnormal_m = sum(1 for w in maintenance_windows.values() if w.status == MaintenanceStatus.ABNORMAL)
    active_m = sum(1 for w in maintenance_windows.values() if w.is_active)
    total_m_alarms = len(maintenance_alarms)

    total_operators = len(_operators)
    qualified_operators = 0
    from models import OperatorGrade
    for op in _operators.values():
        from operator_training import get_operator_qualification
        q = get_operator_qualification(op.operator_id)
        if q.get("is_qualified"):
            qualified_operators += 1
    active_bindings = sum(1 for b in _crane_operator_bindings.values() if b.is_active)
    total_assessments = len(_assessment_records)
    total_handovers = len(_shift_handover_records)

    total_inspection_reports = len(_inspection_reports)
    total_hazards = len(_hazards)
    pending_hazards = sum(1 for h in _hazards.values() if h.status == _HazardStatus.PENDING_RECTIFICATION)
    rectifying_hazards = sum(1 for h in _hazards.values() if h.status == _HazardStatus.RECTIFYING)
    review_hazards = sum(1 for h in _hazards.values() if h.status == _HazardStatus.PENDING_REVIEW)
    closed_hazards = sum(1 for h in _hazards.values() if h.status == _HazardStatus.CLOSED)
    overdue_hazards = sum(1 for h in _hazards.values() if h.is_overdue)

    from cooperative_lift import (
        cooperative_tasks as _cooperative_tasks,
        CooperativeLiftStatus as _CoopStatus,
    )
    total_coop_tasks = len(_cooperative_tasks)
    pending_ready_coop = sum(1 for t in _cooperative_tasks.values() if t.status == _CoopStatus.PENDING_READY)
    synchronizing_coop = sum(1 for t in _cooperative_tasks.values() if t.status == _CoopStatus.SYNCHRONIZING)
    executing_coop = sum(1 for t in _cooperative_tasks.values() if t.status == _CoopStatus.EXECUTING)
    completed_coop = sum(1 for t in _cooperative_tasks.values() if t.status == _CoopStatus.COMPLETED)
    aborted_coop = sum(1 for t in _cooperative_tasks.values() if t.status == _CoopStatus.ABORTED)

    from load_moment_monitor import get_overload_stats, cranes_weight_records
    overload_stats = get_overload_stats()
    total_weight_records = sum(len(records) for records in cranes_weight_records.values())

    from wind_speed_monitor import get_wind_stats, cranes_wind_records
    wind_stats = get_wind_stats()
    total_wind_records = sum(len(records) for records in cranes_wind_records.values())

    from energy_monitor import get_energy_stats, cranes_energy_records
    energy_stats = get_energy_stats()
    total_energy_records = sum(len(records) for records in cranes_energy_records.values())

    from fatigue_monitor import get_fatigue_stats

    energy_warning_alarms = sum(1 for a in alarm_history if a.alarm_type == AlarmType.ENERGY_QUOTA_WARNING)
    energy_exceeded_alarms = sum(1 for a in alarm_history if a.alarm_type == AlarmType.ENERGY_QUOTA_EXCEEDED)
    energy_forecast_alarms = sum(1 for a in alarm_history if a.alarm_type == AlarmType.ENERGY_FORECAST_EXCEEDED)
    energy_limit_recovery_alarms = sum(1 for a in alarm_history if a.alarm_type == AlarmType.ENERGY_LIMIT_RECOVERY)

    fatigue_mild_alarms = sum(1 for a in alarm_history if a.alarm_type == AlarmType.FATIGUE_MILD_WARNING)
    fatigue_severe_alarms = sum(1 for a in alarm_history if a.alarm_type == AlarmType.FATIGUE_SEVERE_WARNING)
    fatigue_forced_alarms = sum(1 for a in alarm_history if a.alarm_type == AlarmType.FATIGUE_FORCED_SHIFTOVER)
    fatigue_recovery_alarms = sum(1 for a in alarm_history if a.alarm_type == AlarmType.FATIGUE_RECOVERY)

    return {
        "status": "ok",
        "service": "塔吊防碰撞联锁服务",
        "cranes_registered": len(cranes_config),
        "total_alarms": len(alarm_history),
        "alarm_breakdown": {
            "collision": collision_alarms,
            "rotation_oscillation": rotation_alarms,
            "trolley_overspeed": overspeed_alarms,
            "load_moment_warning": moment_alarms,
            "wind_speed_warning": wind_warning_alarms,
            "wind_speed_shutdown": wind_shutdown_alarms,
            "energy_quota_warning": energy_warning_alarms,
            "energy_quota_exceeded": energy_exceeded_alarms,
            "energy_forecast_exceeded": energy_forecast_alarms,
            "energy_limit_recovery": energy_limit_recovery_alarms,
            "fatigue_mild_warning": fatigue_mild_alarms,
            "fatigue_severe_warning": fatigue_severe_alarms,
            "fatigue_forced_shiftover": fatigue_forced_alarms,
            "fatigue_recovery": fatigue_recovery_alarms,
        },
        "locked_cranes": sum(1 for l in cranes_lock_status.values() if l.is_locked),
        "frozen_cranes": frozen_cranes,
        "overlap_sectors": len(overlap_sectors),
        "active_tokens": sum(1 for t in token_statuses.values() if t.holder_crane_id),
        "total_window_records": total_window_records,
        "total_anomaly_events": total_anomaly_events,
        "daily_report_stats": {
            "total_reports": total_reports,
            "pending_reports": pending_reports,
            "approved_reports": approved_reports,
            "rejected_reports": rejected_reports,
        },
        "maintenance_stats": {
            "total_windows": total_m_windows,
            "pending_windows": pending_m,
            "in_progress_windows": in_progress_m,
            "completed_windows": completed_m,
            "abnormal_windows": abnormal_m,
            "active_windows": active_m,
            "total_maintenance_alarms": total_m_alarms,
        },
        "total_status_history_records": total_status_records,
        "operator_stats": {
            "total_operators": total_operators,
            "qualified_operators": qualified_operators,
            "active_bindings": active_bindings,
            "total_assessments": total_assessments,
            "total_handovers": total_handovers,
        },
        "inspection_stats": {
            "total_inspection_reports": total_inspection_reports,
            "total_hazards": total_hazards,
            "pending_hazards": pending_hazards,
            "rectifying_hazards": rectifying_hazards,
            "review_hazards": review_hazards,
            "closed_hazards": closed_hazards,
            "overdue_hazards": overdue_hazards,
        },
        "cooperative_lift_stats": {
            "total_tasks": total_coop_tasks,
            "pending_ready": pending_ready_coop,
            "synchronizing": synchronizing_coop,
            "executing": executing_coop,
            "completed": completed_coop,
            "aborted": aborted_coop,
        },
        "load_moment_stats": {
            "total_weight_records_cached": total_weight_records,
            **overload_stats,
        },
        "wind_speed_stats": {
            "total_wind_records_cached": total_wind_records,
            **wind_stats,
        },
        "energy_stats": {
            "total_meter_records_cached": total_energy_records,
            **energy_stats,
        },
        "emergency_stats": {
            "total_rules": len(composite_alarm_rules),
            "enabled_rules": sum(1 for r in composite_alarm_rules.values() if r.enabled),
            "total_events": len(emergency_events),
            "active_events": sum(1 for e in emergency_events.values() if e.status != EmergencyEventStatus.CLOSED),
            "handling_events": sum(1 for e in emergency_events.values() if e.status == EmergencyEventStatus.HANDLING),
            "closed_events": sum(1 for e in emergency_events.values() if e.status == EmergencyEventStatus.CLOSED),
            "general_events": sum(1 for e in emergency_events.values() if e.emergency_level == EmergencyLevel.GENERAL),
            "serious_events": sum(1 for e in emergency_events.values() if e.emergency_level == EmergencyLevel.SERIOUS),
            "critical_events": sum(1 for e in emergency_events.values() if e.emergency_level == EmergencyLevel.CRITICAL),
        },
        "fatigue_stats": {
            **get_fatigue_stats(),
        },
        "timestamp": time.time(),
    }
