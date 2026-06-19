import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from models import (
    FatigueConfig,
    FatigueLevel,
    FatigueAlarmEvent,
    FatigueOperatorStatus,
    AlarmType,
    FatigueDailyStats,
)
from collision import (
    cranes_config,
    cranes_lock_status,
    alarm_history,
    lock_crane,
    unlock_crane_record,
)


_default_fatigue_config = FatigueConfig()

fatigue_configs: Dict[str, FatigueConfig] = {}

operator_fatigue_status: Dict[str, Dict] = {}

fatigue_alarm_events: List[FatigueAlarmEvent] = []

daily_fatigue_stats: Dict[str, Dict[str, FatigueDailyStats]] = {}


def _datetime_str(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _generate_alarm_id() -> str:
    return f"FAT-{uuid.uuid4().hex[:10].upper()}"


def _get_date_str(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def get_operator_fatigue_config(operator_id: str) -> FatigueConfig:
    return fatigue_configs.get(operator_id, _default_fatigue_config)


def get_global_fatigue_config() -> FatigueConfig:
    return _default_fatigue_config.model_copy()


def init_fatigue_monitor_module():
    print(f"[疲劳度监测模块] 初始化完成，默认配置: "
          f"轻度疲劳{_default_fatigue_config.mild_fatigue_hours}小时, "
          f"重度疲劳{_default_fatigue_config.severe_fatigue_hours}小时, "
          f"强制换班{_default_fatigue_config.forced_shiftover_hours}小时, "
          f"休息重置{_default_fatigue_config.rest_reset_minutes}分钟")


def update_fatigue_config(
    operator_id: Optional[str] = None,
    mild_fatigue_hours: Optional[float] = None,
    severe_fatigue_hours: Optional[float] = None,
    forced_shiftover_hours: Optional[float] = None,
    rest_reset_minutes: Optional[float] = None,
) -> Dict:
    global _default_fatigue_config

    if operator_id is not None:
        from operator_training import operators
        if operator_id not in operators:
            raise ValueError(f"操作员 {operator_id} 不存在")

    targets: List[FatigueConfig] = []
    if operator_id is None:
        targets.append(_default_fatigue_config)
        for c in fatigue_configs.values():
            targets.append(c)
    else:
        if operator_id not in fatigue_configs:
            fatigue_configs[operator_id] = _default_fatigue_config.model_copy()
        targets.append(fatigue_configs[operator_id])

    for config in targets:
        if mild_fatigue_hours is not None:
            if mild_fatigue_hours <= 0:
                raise ValueError("轻度疲劳阈值必须大于0")
            config.mild_fatigue_hours = mild_fatigue_hours
        if severe_fatigue_hours is not None:
            if severe_fatigue_hours <= 0:
                raise ValueError("重度疲劳阈值必须大于0")
            config.severe_fatigue_hours = severe_fatigue_hours
        if forced_shiftover_hours is not None:
            if forced_shiftover_hours <= 0:
                raise ValueError("强制换班阈值必须大于0")
            config.forced_shiftover_hours = forced_shiftover_hours
        if rest_reset_minutes is not None:
            if rest_reset_minutes <= 0:
                raise ValueError("休息重置阈值必须大于0")
            config.rest_reset_minutes = rest_reset_minutes

    if (mild_fatigue_hours is not None and severe_fatigue_hours is not None):
        if mild_fatigue_hours >= severe_fatigue_hours:
            raise ValueError("轻度疲劳阈值必须小于重度疲劳阈值")

    if (severe_fatigue_hours is not None and forced_shiftover_hours is not None):
        if severe_fatigue_hours >= forced_shiftover_hours:
            raise ValueError("重度疲劳阈值必须小于强制换班阈值")

    target_desc = f"操作员 {operator_id}" if operator_id else "全局默认配置及所有操作员"
    print(f"[疲劳度监测模块] {target_desc} 疲劳阈值配置已热更新")

    return {
        "success": True,
        "updated_target": target_desc,
        "current_config": get_global_fatigue_config() if operator_id is None else get_operator_fatigue_config(operator_id),
    }


def _create_fatigue_alarm(
    operator_id: str,
    operator_name: str,
    crane_id: str,
    alarm_level: FatigueLevel,
    continuous_work_seconds: float,
    threshold_hours: float,
    message: str,
    now: float,
) -> FatigueAlarmEvent:
    if alarm_level == FatigueLevel.MILD:
        alarm_type = AlarmType.FATIGUE_MILD_WARNING
    elif alarm_level == FatigueLevel.SEVERE:
        alarm_type = AlarmType.FATIGUE_SEVERE_WARNING
    elif alarm_level == FatigueLevel.FORCED_SHIFTOVER:
        alarm_type = AlarmType.FATIGUE_FORCED_SHIFTOVER
    else:
        alarm_type = AlarmType.FATIGUE_RECOVERY

    alarm = FatigueAlarmEvent(
        alarm_id=_generate_alarm_id(),
        alarm_type=alarm_type,
        alarm_level=alarm_level,
        operator_id=operator_id,
        operator_name=operator_name,
        crane_id=crane_id,
        timestamp=now,
        datetime_str=_datetime_str(now),
        continuous_work_seconds=continuous_work_seconds,
        threshold_hours=threshold_hours,
        message=message,
        details={
            "mild_threshold_hours": get_operator_fatigue_config(operator_id).mild_fatigue_hours,
            "severe_threshold_hours": get_operator_fatigue_config(operator_id).severe_fatigue_hours,
            "forced_shiftover_threshold_hours": get_operator_fatigue_config(operator_id).forced_shiftover_hours,
        },
    )

    fatigue_alarm_events.append(alarm)

    try:
        from models import AlarmEvent
        generic_alarm = AlarmEvent(
            alarm_id=alarm.alarm_id,
            alarm_type=alarm_type,
            timestamp=alarm.timestamp,
            datetime_str=alarm.datetime_str,
            crane_a_id=crane_id,
            crane_b_id="",
            message=alarm.message,
            details={
                "operator_id": operator_id,
                "operator_name": operator_name,
                "continuous_work_seconds": continuous_work_seconds,
                "threshold_hours": threshold_hours,
                "alarm_level": alarm_level.value,
            },
        )
        alarm_history.append(generic_alarm)
    except Exception as e:
        print(f"[疲劳度监测模块] 写入通用告警历史失败: {e}")

    date_str = _get_date_str(now)
    if date_str not in daily_fatigue_stats:
        daily_fatigue_stats[date_str] = {}
    if operator_id not in daily_fatigue_stats[date_str]:
        daily_fatigue_stats[date_str][operator_id] = FatigueDailyStats(
            operator_id=operator_id,
            operator_name=operator_name,
        )
    stats = daily_fatigue_stats[date_str][operator_id]
    if alarm_level == FatigueLevel.MILD:
        stats.mild_warning_count += 1
    elif alarm_level == FatigueLevel.SEVERE:
        stats.severe_warning_count += 1
    elif alarm_level == FatigueLevel.FORCED_SHIFTOVER:
        stats.forced_shiftover_count += 1

    return alarm


def _update_daily_max_continuous(operator_id: str, operator_name: str, continuous_seconds: float, now: float):
    date_str = _get_date_str(now)
    if date_str not in daily_fatigue_stats:
        daily_fatigue_stats[date_str] = {}
    if operator_id not in daily_fatigue_stats[date_str]:
        daily_fatigue_stats[date_str][operator_id] = FatigueDailyStats(
            operator_id=operator_id,
            operator_name=operator_name,
        )
    stats = daily_fatigue_stats[date_str][operator_id]
    if continuous_seconds > stats.max_continuous_work_seconds:
        stats.max_continuous_work_seconds = continuous_seconds


def init_operator_fatigue(operator_id: str, operator_name: str, crane_id: str, bound_at: float):
    operator_fatigue_status[operator_id] = {
        "operator_id": operator_id,
        "operator_name": operator_name,
        "crane_id": crane_id,
        "current_fatigue_level": FatigueLevel.NORMAL,
        "continuous_work_start": bound_at,
        "continuous_work_seconds": 0.0,
        "last_status_report_at": bound_at,
        "last_rest_at": None,
        "bound_at": bound_at,
        "is_forced_shiftover": False,
        "forced_shiftover_at": None,
        "last_mild_alarm_at": None,
        "last_severe_alarm_at": None,
        "last_forced_alarm_at": None,
    }


def reset_operator_fatigue(operator_id: str):
    if operator_id in operator_fatigue_status:
        del operator_fatigue_status[operator_id]


def _check_rest_and_reset(operator_id: str, now: float) -> bool:
    status = operator_fatigue_status.get(operator_id)
    if not status:
        return False

    config = get_operator_fatigue_config(operator_id)
    rest_reset_seconds = config.rest_reset_minutes * 60

    last_report = status.get("last_status_report_at")
    if last_report is None:
        return False

    idle_duration = now - last_report
    if idle_duration >= rest_reset_seconds:
        status["continuous_work_start"] = now
        status["continuous_work_seconds"] = 0.0
        status["last_rest_at"] = last_report
        status["last_status_report_at"] = now

        if status["current_fatigue_level"] != FatigueLevel.NORMAL:
            status["current_fatigue_level"] = FatigueLevel.NORMAL
            status["is_forced_shiftover"] = False
            status["forced_shiftover_at"] = None

            crane_id = status.get("crane_id")
            if crane_id and crane_id in cranes_lock_status:
                lock = cranes_lock_status[crane_id]
                if lock.is_locked and lock.locked_reason and "疲劳" in lock.locked_reason:
                    unlock_crane_record(crane_id)
                    lock.is_locked = False
                    lock.locked_reason = None
                    lock.locked_at = None

            _create_fatigue_alarm(
                operator_id=operator_id,
                operator_name=status["operator_name"],
                crane_id=crane_id or "",
                alarm_level=FatigueLevel.NORMAL,
                continuous_work_seconds=0.0,
                threshold_hours=config.rest_reset_minutes / 60,
                message=f"操作员 {status['operator_name']} 休息{config.rest_reset_minutes:.0f}分钟以上，疲劳状态已重置",
                now=now,
            )
            print(f"[疲劳度监测模块] 操作员 {operator_id} 休息超时，疲劳状态已重置")

        return True

    return False


def process_status_report_for_fatigue(crane_id: str, now: float) -> Dict:
    from operator_training import crane_operator_bindings, operators

    binding = crane_operator_bindings.get(crane_id)
    if not binding or not binding.is_active:
        return {
            "has_operator": False,
            "fatigue_level": FatigueLevel.NORMAL,
            "message": "塔吊无操作员在岗",
        }

    operator_id = binding.operator_id
    operator = operators.get(operator_id)
    if not operator:
        return {
            "has_operator": False,
            "fatigue_level": FatigueLevel.NORMAL,
            "message": "操作员信息不存在",
        }

    if operator_id not in operator_fatigue_status:
        init_operator_fatigue(operator_id, operator.name, crane_id, binding.bound_at)

    status = operator_fatigue_status[operator_id]
    config = get_operator_fatigue_config(operator_id)

    _check_rest_and_reset(operator_id, now)
    status = operator_fatigue_status.get(operator_id)
    if not status:
        return {
            "has_operator": False,
            "fatigue_level": FatigueLevel.NORMAL,
            "message": "操作员状态异常",
        }

    status["last_status_report_at"] = now
    continuous_seconds = now - status["continuous_work_start"]
    status["continuous_work_seconds"] = continuous_seconds

    _update_daily_max_continuous(operator_id, operator.name, continuous_seconds, now)

    result = {
        "has_operator": True,
        "operator_id": operator_id,
        "operator_name": operator.name,
        "crane_id": crane_id,
        "continuous_work_seconds": continuous_seconds,
        "fatigue_level": status["current_fatigue_level"],
        "is_forced_shiftover": status["is_forced_shiftover"],
        "alarm_triggered": False,
    }

    forced_shiftover_seconds = config.forced_shiftover_hours * 3600
    severe_seconds = config.severe_fatigue_hours * 3600
    mild_seconds = config.mild_fatigue_hours * 3600

    if continuous_seconds >= forced_shiftover_seconds and not status["is_forced_shiftover"]:
        status["current_fatigue_level"] = FatigueLevel.FORCED_SHIFTOVER
        status["is_forced_shiftover"] = True
        status["forced_shiftover_at"] = now
        status["last_forced_alarm_at"] = now

        message = (f"强制换班告警: 操作员 {operator.name} 已连续作业 {continuous_seconds/3600:.1f} 小时，"
                   f"超过强制换班阈值 {config.forced_shiftover_hours} 小时，塔吊已锁定，请立即换班")

        _create_fatigue_alarm(
            operator_id=operator_id,
            operator_name=operator.name,
            crane_id=crane_id,
            alarm_level=FatigueLevel.FORCED_SHIFTOVER,
            continuous_work_seconds=continuous_seconds,
            threshold_hours=config.forced_shiftover_hours,
            message=message,
            now=now,
        )

        lock_crane(crane_id, f"操作员疲劳强制换班: 连续作业{continuous_seconds/3600:.1f}小时")

        result["alarm_triggered"] = True
        result["alarm_type"] = "FORCED_SHIFTOVER"
        result["fatigue_level"] = FatigueLevel.FORCED_SHIFTOVER
        result["is_forced_shiftover"] = True
        result["message"] = message

        print(f"[疲劳度监测模块] 塔吊 {crane_id} 操作员 {operator_id} 触发强制换班，"
              f"连续作业 {continuous_seconds/3600:.1f} 小时")

    elif continuous_seconds >= severe_seconds and status["current_fatigue_level"] != FatigueLevel.SEVERE \
            and status["current_fatigue_level"] != FatigueLevel.FORCED_SHIFTOVER:
        status["current_fatigue_level"] = FatigueLevel.SEVERE
        status["last_severe_alarm_at"] = now

        message = (f"重度疲劳警告: 操作员 {operator.name} 已连续作业 {continuous_seconds/3600:.1f} 小时，"
                   f"超过重度疲劳阈值 {config.severe_fatigue_hours} 小时，限制接收新工单")

        _create_fatigue_alarm(
            operator_id=operator_id,
            operator_name=operator.name,
            crane_id=crane_id,
            alarm_level=FatigueLevel.SEVERE,
            continuous_work_seconds=continuous_seconds,
            threshold_hours=config.severe_fatigue_hours,
            message=message,
            now=now,
        )

        result["alarm_triggered"] = True
        result["alarm_type"] = "SEVERE"
        result["fatigue_level"] = FatigueLevel.SEVERE
        result["message"] = message

        print(f"[疲劳度监测模块] 塔吊 {crane_id} 操作员 {operator_id} 触发重度疲劳警告，"
              f"连续作业 {continuous_seconds/3600:.1f} 小时")

    elif continuous_seconds >= mild_seconds and status["current_fatigue_level"] != FatigueLevel.MILD \
            and status["current_fatigue_level"] != FatigueLevel.SEVERE \
            and status["current_fatigue_level"] != FatigueLevel.FORCED_SHIFTOVER:
        status["current_fatigue_level"] = FatigueLevel.MILD
        status["last_mild_alarm_at"] = now

        message = (f"轻度疲劳提醒: 操作员 {operator.name} 已连续作业 {continuous_seconds/3600:.1f} 小时，"
                   f"超过轻度疲劳阈值 {config.mild_fatigue_hours} 小时，请注意休息")

        _create_fatigue_alarm(
            operator_id=operator_id,
            operator_name=operator.name,
            crane_id=crane_id,
            alarm_level=FatigueLevel.MILD,
            continuous_work_seconds=continuous_seconds,
            threshold_hours=config.mild_fatigue_hours,
            message=message,
            now=now,
        )

        result["alarm_triggered"] = True
        result["alarm_type"] = "MILD"
        result["fatigue_level"] = FatigueLevel.MILD
        result["message"] = message

        print(f"[疲劳度监测模块] 塔吊 {crane_id} 操作员 {operator_id} 触发轻度疲劳提醒，"
              f"连续作业 {continuous_seconds/3600:.1f} 小时")

    return result


def get_operator_fatigue_status(operator_id: str) -> Optional[FatigueOperatorStatus]:
    from operator_training import operators

    operator = operators.get(operator_id)
    if not operator:
        return None

    status = operator_fatigue_status.get(operator_id)
    if not status:
        from operator_training import operator_crane_bindings, crane_operator_bindings
        crane_id = operator_crane_bindings.get(operator_id)
        binding = crane_operator_bindings.get(crane_id) if crane_id else None
        bound_at = binding.bound_at if binding else None

        return FatigueOperatorStatus(
            operator_id=operator_id,
            operator_name=operator.name,
            crane_id=crane_id,
            current_fatigue_level=FatigueLevel.NORMAL,
            continuous_work_seconds=0.0,
            last_status_report_at=None,
            last_rest_at=None,
            bound_at=bound_at,
            is_forced_shiftover=False,
            forced_shiftover_at=None,
        )

    return FatigueOperatorStatus(
        operator_id=operator_id,
        operator_name=status["operator_name"],
        crane_id=status.get("crane_id"),
        current_fatigue_level=status["current_fatigue_level"],
        continuous_work_seconds=status["continuous_work_seconds"],
        last_status_report_at=status.get("last_status_report_at"),
        last_rest_at=status.get("last_rest_at"),
        bound_at=status.get("bound_at"),
        is_forced_shiftover=status["is_forced_shiftover"],
        forced_shiftover_at=status.get("forced_shiftover_at"),
    )


def get_all_operator_fatigue_status() -> List[FatigueOperatorStatus]:
    from operator_training import operators

    result = []
    for operator_id in operators.keys():
        status = get_operator_fatigue_status(operator_id)
        if status:
            result.append(status)
    return result


def get_fatigue_alarm_history(
    operator_id: Optional[str] = None,
    crane_id: Optional[str] = None,
    alarm_level: Optional[FatigueLevel] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    limit: int = 200,
) -> List[FatigueAlarmEvent]:
    result = fatigue_alarm_events
    if operator_id:
        result = [a for a in result if a.operator_id == operator_id]
    if crane_id:
        result = [a for a in result if a.crane_id == crane_id]
    if alarm_level:
        result = [a for a in result if a.alarm_level == alarm_level]
    if start_time is not None:
        result = [a for a in result if a.timestamp >= start_time]
    if end_time is not None:
        result = [a for a in result if a.timestamp <= end_time]
    return result[-limit:]


def is_operator_severely_fatigued(operator_id: str) -> bool:
    status = operator_fatigue_status.get(operator_id)
    if not status:
        return False
    return status["current_fatigue_level"] in (FatigueLevel.SEVERE, FatigueLevel.FORCED_SHIFTOVER)


def is_operator_forced_shiftover(operator_id: str) -> bool:
    status = operator_fatigue_status.get(operator_id)
    if not status:
        return False
    return status["is_forced_shiftover"]


def release_forced_shiftover(operator_id: str) -> Dict:
    from operator_training import operators, operator_crane_bindings

    operator = operators.get(operator_id)
    if not operator:
        return {"error": f"操作员 {operator_id} 不存在"}

    status = operator_fatigue_status.get(operator_id)
    if not status or not status["is_forced_shiftover"]:
        return {"error": f"操作员 {operator_id} 未处于强制换班状态"}

    crane_id = status.get("crane_id")
    now = time.time()
    config = get_operator_fatigue_config(operator_id)

    status["is_forced_shiftover"] = False
    status["forced_shiftover_at"] = None
    status["current_fatigue_level"] = FatigueLevel.NORMAL
    status["continuous_work_start"] = now
    status["continuous_work_seconds"] = 0.0
    status["last_rest_at"] = now

    if crane_id and crane_id in cranes_lock_status:
        lock = cranes_lock_status[crane_id]
        if lock.is_locked and lock.locked_reason and "疲劳" in lock.locked_reason:
            unlock_crane_record(crane_id)
            lock.is_locked = False
            lock.locked_reason = None
            lock.locked_at = None

    _create_fatigue_alarm(
        operator_id=operator_id,
        operator_name=operator.name,
        crane_id=crane_id or "",
        alarm_level=FatigueLevel.NORMAL,
        continuous_work_seconds=0.0,
        threshold_hours=config.forced_shiftover_hours,
        message=f"操作员 {operator.name} 已完成换班，疲劳状态重置，塔吊恢复正常",
        now=now,
    )

    print(f"[疲劳度监测模块] 操作员 {operator_id} 强制换班状态已解除")

    return {
        "success": True,
        "message": "强制换班状态已解除，疲劳状态已重置",
        "operator_id": operator_id,
        "crane_id": crane_id,
    }


def get_daily_fatigue_stats(date_str: str) -> Dict[str, FatigueDailyStats]:
    return daily_fatigue_stats.get(date_str, {})


def get_operator_daily_fatigue_stats(operator_id: str, date_str: str) -> Optional[FatigueDailyStats]:
    day_stats = daily_fatigue_stats.get(date_str, {})
    return day_stats.get(operator_id)


def get_fatigue_stats() -> Dict:
    total_events = len(fatigue_alarm_events)
    mild_count = sum(1 for e in fatigue_alarm_events if e.alarm_level == FatigueLevel.MILD)
    severe_count = sum(1 for e in fatigue_alarm_events if e.alarm_level == FatigueLevel.SEVERE)
    forced_count = sum(1 for e in fatigue_alarm_events if e.alarm_level == FatigueLevel.FORCED_SHIFTOVER)
    recovery_count = sum(1 for e in fatigue_alarm_events if e.alarm_level == FatigueLevel.NORMAL)

    current_forced = sum(1 for s in operator_fatigue_status.values() if s["is_forced_shiftover"])
    current_severe = sum(1 for s in operator_fatigue_status.values() if s["current_fatigue_level"] == FatigueLevel.SEVERE)
    current_mild = sum(1 for s in operator_fatigue_status.values() if s["current_fatigue_level"] == FatigueLevel.MILD)

    return {
        "total_fatigue_events": total_events,
        "mild_warning_count": mild_count,
        "severe_warning_count": severe_count,
        "forced_shiftover_count": forced_count,
        "recovery_count": recovery_count,
        "current_mild_fatigue": current_mild,
        "current_severe_fatigue": current_severe,
        "current_forced_shiftover": current_forced,
    }
