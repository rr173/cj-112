import time
import uuid
from datetime import datetime
from collections import deque
from typing import Dict, List, Optional

from models import (
    WindSpeedConfig,
    WindSpeedReport,
    WindSpeedRecord,
    WindSpeedAlarmEvent,
    WindSpeedRecoveryEvent,
    WindSpeedStatus,
    WindAlarmLevel,
    AlarmType,
)
from collision import (
    cranes_config,
    cranes_lock_status,
    alarm_history,
    lock_crane,
    unlock_crane_record,
)


_default_wind_config = WindSpeedConfig()

cranes_wind_config: Dict[str, WindSpeedConfig] = {}

cranes_wind_records: Dict[str, deque] = {}

cranes_wind_alarms: List[WindSpeedAlarmEvent] = []

cranes_wind_recovery_events: List[WindSpeedRecoveryEvent] = []

cranes_wind_shutdown_status: Dict[str, Dict] = {}


def _datetime_str(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def get_crane_wind_config(crane_id: str) -> WindSpeedConfig:
    return cranes_wind_config.get(crane_id, _default_wind_config)


def get_global_wind_config() -> WindSpeedConfig:
    return _default_wind_config.model_copy()


def init_wind_speed_monitor_module():
    from collision import cranes_config as _cranes_config
    for crane_id in _cranes_config.keys():
        if crane_id not in cranes_wind_config:
            cranes_wind_config[crane_id] = _default_wind_config.model_copy()
        if crane_id not in cranes_wind_records:
            config = get_crane_wind_config(crane_id)
            cranes_wind_records[crane_id] = deque(maxlen=config.max_records_per_crane)
        if crane_id not in cranes_wind_shutdown_status:
            cranes_wind_shutdown_status[crane_id] = {
                "is_shutdown": False,
                "shutdown_at": None,
                "shutdown_reason": None,
                "max_wind_speed": 0.0,
                "consecutive_normal_count": 0,
            }
    print(f"[风速监测模块] 初始化完成，已为 {len(cranes_wind_config)} 台塔吊加载风速配置")


def update_wind_config(
    crane_id: Optional[str] = None,
    shutdown_threshold: Optional[float] = None,
    warning_threshold: Optional[float] = None,
    avg_window_seconds: Optional[int] = None,
    auto_recovery_consecutive_count: Optional[int] = None,
    auto_recovery_threshold_ratio: Optional[float] = None,
    max_records_per_crane: Optional[int] = None,
) -> Dict:
    global _default_wind_config

    if crane_id is not None and crane_id not in cranes_config:
        raise ValueError(f"塔吊 {crane_id} 不存在")

    targets: List[WindSpeedConfig] = []
    if crane_id is None:
        targets.append(_default_wind_config)
        for cid in cranes_wind_config:
            targets.append(cranes_wind_config[cid])
    else:
        if crane_id not in cranes_wind_config:
            cranes_wind_config[crane_id] = _default_wind_config.model_copy()
        targets.append(cranes_wind_config[crane_id])

    for config in targets:
        if shutdown_threshold is not None:
            if shutdown_threshold <= 0:
                raise ValueError("停机阈值必须大于0")
            config.shutdown_threshold = shutdown_threshold
        if warning_threshold is not None:
            if warning_threshold <= 0:
                raise ValueError("预警阈值必须大于0")
            config.warning_threshold = warning_threshold
        if avg_window_seconds is not None:
            if avg_window_seconds <= 0:
                raise ValueError("平均窗口秒数必须大于0")
            config.avg_window_seconds = avg_window_seconds
        if auto_recovery_consecutive_count is not None:
            if auto_recovery_consecutive_count <= 0:
                raise ValueError("自动恢复连续计数必须大于0")
            config.auto_recovery_consecutive_count = auto_recovery_consecutive_count
        if auto_recovery_threshold_ratio is not None:
            if not (0 < auto_recovery_threshold_ratio <= 1):
                raise ValueError("自动恢复阈值比例必须在(0, 1]之间")
            config.auto_recovery_threshold_ratio = auto_recovery_threshold_ratio
        if max_records_per_crane is not None:
            if max_records_per_crane <= 0:
                raise ValueError("最大记录数必须大于0")
            config.max_records_per_crane = max_records_per_crane
            if crane_id and crane_id in cranes_wind_records:
                cranes_wind_records[crane_id] = deque(
                    list(cranes_wind_records[crane_id])[-max_records_per_crane:],
                    maxlen=max_records_per_crane
                )

    if warning_threshold is not None and shutdown_threshold is not None:
        if warning_threshold >= shutdown_threshold:
            raise ValueError("预警阈值必须小于停机阈值")

    target_desc = f"塔吊 {crane_id}" if crane_id else "全局默认配置及所有塔吊"
    print(f"[风速监测模块] {target_desc} 风速配置已热更新")

    return {
        "success": True,
        "updated_target": target_desc,
        "current_config": get_global_wind_config() if crane_id is None else get_crane_wind_config(crane_id),
    }


def calculate_avg_wind_speed(crane_id: str, window_seconds: int) -> Optional[float]:
    records = cranes_wind_records.get(crane_id)
    if not records or len(records) == 0:
        return None

    now = time.time()
    cutoff_time = now - window_seconds

    recent_records = [r for r in records if r.sensor_timestamp >= cutoff_time]
    if not recent_records:
        return None

    return sum(r.wind_speed for r in recent_records) / len(recent_records)


def is_crane_wind_shutdown(crane_id: str) -> bool:
    status = cranes_wind_shutdown_status.get(crane_id)
    return status.get("is_shutdown", False) if status else False


def get_crane_wind_shutdown_info(crane_id: str) -> Optional[Dict]:
    status = cranes_wind_shutdown_status.get(crane_id)
    if not status or not status.get("is_shutdown"):
        return None
    return status


def _create_wind_alarm(
    crane_id: str,
    alarm_level: WindAlarmLevel,
    wind_speed: float,
    avg_wind_speed: Optional[float],
    threshold: float,
    message: str,
    now: float,
) -> WindSpeedAlarmEvent:
    alarm_type = AlarmType.WIND_SPEED_SHUTDOWN if alarm_level == WindAlarmLevel.SHUTDOWN else AlarmType.WIND_SPEED_WARNING

    alarm = WindSpeedAlarmEvent(
        alarm_id=f"WIND-{uuid.uuid4().hex[:12].upper()}",
        alarm_type=alarm_type,
        alarm_level=alarm_level,
        crane_id=crane_id,
        timestamp=now,
        datetime_str=_datetime_str(now),
        wind_speed=wind_speed,
        avg_wind_speed_60s=avg_wind_speed,
        threshold=threshold,
        message=message,
        details={
            "shutdown_threshold": get_crane_wind_config(crane_id).shutdown_threshold,
            "warning_threshold": get_crane_wind_config(crane_id).warning_threshold,
        },
    )

    cranes_wind_alarms.append(alarm)

    try:
        from models import AlarmEvent, CoordinateSnapshot
        generic_alarm = AlarmEvent(
            alarm_id=alarm.alarm_id,
            alarm_type=alarm_type,
            timestamp=alarm.timestamp,
            datetime_str=alarm.datetime_str,
            crane_a_id=crane_id,
            crane_b_id="",
            message=alarm.message,
            details={
                "wind_speed": wind_speed,
                "avg_wind_speed_60s": avg_wind_speed,
                "threshold": threshold,
                "alarm_level": alarm_level.value,
            },
        )
        alarm_history.append(generic_alarm)
    except Exception as e:
        print(f"[风速监测模块] 写入通用告警历史失败: {e}")

    return alarm


def _trigger_shutdown(crane_id: str, wind_speed: float, now: float, threshold: float) -> WindSpeedAlarmEvent:
    message = f"风速停机告警: 瞬时风速 {wind_speed:.1f} 米/秒 超过停机阈值 {threshold:.1f} 米/秒，塔吊已锁定"
    avg_speed = calculate_avg_wind_speed(crane_id, get_crane_wind_config(crane_id).avg_window_seconds)

    alarm = _create_wind_alarm(
        crane_id=crane_id,
        alarm_level=WindAlarmLevel.SHUTDOWN,
        wind_speed=wind_speed,
        avg_wind_speed=avg_speed,
        threshold=threshold,
        message=message,
        now=now,
    )

    lock_crane(crane_id, f"风速超限停机: 瞬时风速 {wind_speed:.1f} 米/秒")

    status = cranes_wind_shutdown_status[crane_id]
    status["is_shutdown"] = True
    status["shutdown_at"] = now
    status["shutdown_reason"] = message
    status["max_wind_speed"] = wind_speed
    status["consecutive_normal_count"] = 0

    print(f"[风速监测模块] 塔吊 {crane_id} 触发风速停机，风速: {wind_speed:.1f} m/s，阈值: {threshold:.1f} m/s")

    return alarm


def _trigger_warning(crane_id: str, avg_wind_speed: float, now: float, threshold: float) -> Optional[WindSpeedAlarmEvent]:
    records = cranes_wind_records.get(crane_id, [])
    latest_speed = records[-1].wind_speed if records else 0.0

    message = f"风速预警: 最近60秒平均风速 {avg_wind_speed:.1f} 米/秒 超过预警阈值 {threshold:.1f} 米/秒"

    recent_warnings = [
        a for a in cranes_wind_alarms
        if a.crane_id == crane_id
        and a.alarm_level == WindAlarmLevel.WARNING
        and now - a.timestamp < 60
    ]
    if recent_warnings:
        return None

    alarm = _create_wind_alarm(
        crane_id=crane_id,
        alarm_level=WindAlarmLevel.WARNING,
        wind_speed=latest_speed,
        avg_wind_speed=avg_wind_speed,
        threshold=threshold,
        message=message,
        now=now,
    )

    print(f"[风速监测模块] 塔吊 {crane_id} 触发风速预警，60秒平均: {avg_wind_speed:.1f} m/s，阈值: {threshold:.1f} m/s")

    return alarm


def _auto_recovery_check(crane_id: str, wind_speed: float, now: float) -> Optional[WindSpeedRecoveryEvent]:
    status = cranes_wind_shutdown_status.get(crane_id)
    if not status or not status.get("is_shutdown"):
        return None

    config = get_crane_wind_config(crane_id)
    auto_recovery_threshold = config.shutdown_threshold * config.auto_recovery_threshold_ratio

    if wind_speed < auto_recovery_threshold:
        status["consecutive_normal_count"] += 1
    else:
        status["consecutive_normal_count"] = 0
        if wind_speed > status["max_wind_speed"]:
            status["max_wind_speed"] = wind_speed
        return None

    if status["consecutive_normal_count"] >= config.auto_recovery_consecutive_count:
        return _release_wind_shutdown(crane_id, "AUTO", now)

    return None


def _release_wind_shutdown(crane_id: str, method: str, now: float) -> WindSpeedRecoveryEvent:
    status = cranes_wind_shutdown_status.get(crane_id)
    if not status or not status.get("is_shutdown"):
        raise ValueError(f"塔吊 {crane_id} 未处于风速停机状态")

    config = get_crane_wind_config(crane_id)
    avg_speed = calculate_avg_wind_speed(crane_id, config.avg_window_seconds) or 0.0
    shutdown_at = status.get("shutdown_at", now)
    duration = now - shutdown_at

    method_desc = "系统自动恢复" if method == "AUTO" else "人工手动解除"
    message = f"{method_desc}: 塔吊已从风速停机状态恢复，停机期间最大风速 {status['max_wind_speed']:.1f} m/s，恢复前平均风速 {avg_speed:.1f} m/s，停机时长 {duration:.0f} 秒"

    recovery = WindSpeedRecoveryEvent(
        recovery_id=f"WND-REC-{uuid.uuid4().hex[:10].upper()}",
        crane_id=crane_id,
        recovery_time=now,
        recovery_datetime_str=_datetime_str(now),
        max_wind_speed_during_shutdown=status["max_wind_speed"],
        avg_wind_speed_before_recovery=avg_speed,
        shutdown_duration_seconds=duration,
        recovery_method=method,
        message=message,
    )

    cranes_wind_recovery_events.append(recovery)

    unlock_crane_record(crane_id)
    lock = cranes_lock_status.get(crane_id)
    if lock:
        lock.is_locked = False
        lock.locked_reason = None
        lock.locked_at = None

    status["is_shutdown"] = False
    status["shutdown_at"] = None
    status["shutdown_reason"] = None
    status["max_wind_speed"] = 0.0
    status["consecutive_normal_count"] = 0

    print(f"[风速监测模块] 塔吊 {crane_id} 风速停机状态已解除（{method_desc}），停机时长: {duration:.0f} 秒")

    return recovery


def manual_release_wind_shutdown(crane_id: str) -> Dict:
    if crane_id not in cranes_config:
        return {"error": f"塔吊 {crane_id} 不存在"}

    status = cranes_wind_shutdown_status.get(crane_id)
    if not status or not status.get("is_shutdown"):
        return {"error": f"塔吊 {crane_id} 未处于风速停机状态"}

    now = time.time()
    try:
        recovery = _release_wind_shutdown(crane_id, "MANUAL", now)
        return {
            "success": True,
            "message": "风速停机状态已人工解除",
            "recovery_event": recovery,
        }
    except ValueError as e:
        return {"error": str(e)}


def process_wind_speed_report(report: WindSpeedReport) -> Dict:
    if report.crane_id not in cranes_config:
        raise ValueError(f"塔吊 {report.crane_id} 不存在")
    if report.wind_speed < 0:
        raise ValueError("风速值不能为负数")

    now = time.time()
    crane_id = report.crane_id
    config = get_crane_wind_config(crane_id)

    if crane_id not in cranes_wind_records:
        cranes_wind_records[crane_id] = deque(maxlen=config.max_records_per_crane)
    if crane_id not in cranes_wind_shutdown_status:
        cranes_wind_shutdown_status[crane_id] = {
            "is_shutdown": False,
            "shutdown_at": None,
            "shutdown_reason": None,
            "max_wind_speed": 0.0,
            "consecutive_normal_count": 0,
        }

    wind_record = WindSpeedRecord(
        crane_id=crane_id,
        wind_speed=report.wind_speed,
        sensor_timestamp=report.sensor_timestamp,
        received_at=now,
        datetime_str=_datetime_str(report.sensor_timestamp),
    )
    cranes_wind_records[crane_id].append(wind_record)

    is_shutdown = is_crane_wind_shutdown(crane_id)
    result = {
        "recorded": True,
        "crane_id": crane_id,
        "wind_speed": report.wind_speed,
        "is_wind_shutdown": is_shutdown,
        "alarm_triggered": False,
        "recovery_triggered": False,
    }

    if is_shutdown:
        status = cranes_wind_shutdown_status[crane_id]
        if report.wind_speed > status["max_wind_speed"]:
            status["max_wind_speed"] = report.wind_speed

        recovery = _auto_recovery_check(crane_id, report.wind_speed, now)
        if recovery:
            result["recovery_triggered"] = True
            result["recovery_event"] = recovery
            result["is_wind_shutdown"] = False
        else:
            result["consecutive_normal_count"] = status["consecutive_normal_count"]
            result["auto_recovery_threshold"] = config.shutdown_threshold * config.auto_recovery_threshold_ratio
        return result

    shutdown_alarm = None
    warning_alarm = None

    if report.wind_speed >= config.shutdown_threshold:
        shutdown_alarm = _trigger_shutdown(crane_id, report.wind_speed, now, config.shutdown_threshold)
        result["alarm_triggered"] = True
        result["shutdown_alarm"] = shutdown_alarm
        result["is_wind_shutdown"] = True
    else:
        avg_speed = calculate_avg_wind_speed(crane_id, config.avg_window_seconds)
        if avg_speed is not None and avg_speed >= config.warning_threshold:
            warning_alarm = _trigger_warning(crane_id, avg_speed, now, config.warning_threshold)
            if warning_alarm:
                result["alarm_triggered"] = True
                result["warning_alarm"] = warning_alarm
        result["avg_wind_speed_60s"] = avg_speed

    return result


def get_wind_speed_history(
    crane_id: str,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    limit: int = 120,
) -> List[WindSpeedRecord]:
    if crane_id not in cranes_wind_records:
        return []
    records = list(cranes_wind_records[crane_id])
    if start_time is not None:
        records = [r for r in records if r.sensor_timestamp >= start_time]
    if end_time is not None:
        records = [r for r in records if r.sensor_timestamp <= end_time]
    return records[-limit:]


def get_wind_alarm_history(
    crane_id: Optional[str] = None,
    alarm_level: Optional[WindAlarmLevel] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    limit: int = 200,
) -> List[WindSpeedAlarmEvent]:
    result = cranes_wind_alarms
    if crane_id:
        result = [a for a in result if a.crane_id == crane_id]
    if alarm_level:
        result = [a for a in result if a.alarm_level == alarm_level]
    if start_time is not None:
        result = [a for a in result if a.timestamp >= start_time]
    if end_time is not None:
        result = [a for a in result if a.timestamp <= end_time]
    return result[-limit:]


def get_wind_recovery_history(
    crane_id: Optional[str] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    limit: int = 100,
) -> List[WindSpeedRecoveryEvent]:
    result = cranes_wind_recovery_events
    if crane_id:
        result = [r for r in result if r.crane_id == crane_id]
    if start_time is not None:
        result = [r for r in result if r.recovery_time >= start_time]
    if end_time is not None:
        result = [r for r in result if r.recovery_time <= end_time]
    return result[-limit:]


def get_wind_speed_status(crane_id: str) -> Optional[WindSpeedStatus]:
    if crane_id not in cranes_config:
        return None

    config = get_crane_wind_config(crane_id)
    records = cranes_wind_records.get(crane_id, [])
    latest_record = records[-1] if records else None

    avg_speed = calculate_avg_wind_speed(crane_id, config.avg_window_seconds)

    shutdown_status = cranes_wind_shutdown_status.get(crane_id, {})

    return WindSpeedStatus(
        crane_id=crane_id,
        latest_wind_speed=latest_record.wind_speed if latest_record else None,
        latest_sensor_timestamp=latest_record.sensor_timestamp if latest_record else None,
        latest_datetime_str=latest_record.datetime_str if latest_record else None,
        avg_wind_speed_60s=avg_speed,
        is_wind_shutdown=shutdown_status.get("is_shutdown", False),
        wind_shutdown_at=shutdown_status.get("shutdown_at"),
        wind_shutdown_reason=shutdown_status.get("shutdown_reason"),
        consecutive_normal_count=shutdown_status.get("consecutive_normal_count", 0),
        current_config=config,
    )


def get_all_wind_speed_statuses() -> List[WindSpeedStatus]:
    result = []
    for crane_id in cranes_config.keys():
        status = get_wind_speed_status(crane_id)
        if status:
            result.append(status)
    return result


def get_wind_stats() -> Dict:
    total_alarms = len(cranes_wind_alarms)
    warnings = sum(1 for a in cranes_wind_alarms if a.alarm_level == WindAlarmLevel.WARNING)
    shutdowns = sum(1 for a in cranes_wind_alarms if a.alarm_level == WindAlarmLevel.SHUTDOWN)
    total_recoveries = len(cranes_wind_recovery_events)
    auto_recoveries = sum(1 for r in cranes_wind_recovery_events if r.recovery_method == "AUTO")
    manual_recoveries = sum(1 for r in cranes_wind_recovery_events if r.recovery_method == "MANUAL")
    current_shutdowns = sum(1 for s in cranes_wind_shutdown_status.values() if s.get("is_shutdown"))

    return {
        "total_alarms": total_alarms,
        "warning_count": warnings,
        "shutdown_count": shutdowns,
        "total_recoveries": total_recoveries,
        "auto_recoveries": auto_recoveries,
        "manual_recoveries": manual_recoveries,
        "current_shutdowns": current_shutdowns,
    }
