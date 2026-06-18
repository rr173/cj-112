import time
import uuid
from datetime import datetime
from collections import deque
from typing import Dict, List, Optional

from models import (
    LoadMomentEnvelopePoint,
    OverloadAlarmLevel,
    OverloadAlarmEvent,
    WeightRecord,
    WeightSensorReport,
    RealtimeLoadStatus,
)
from collision import (
    cranes_config,
    cranes_current_status,
    cranes_lock_status,
    lock_crane,
)


MAX_WEIGHT_RECORDS_PER_CRANE = 300

WEIGHT_HISTORY_WINDOW_SECONDS = 60


cranes_envelope_curves: Dict[str, List[LoadMomentEnvelopePoint]] = {}

cranes_weight_records: Dict[str, deque] = {}

cranes_overload_alarms: List[OverloadAlarmEvent] = []

emergency_notification_callbacks: List = []


PRESET_ENVELOPES: Dict[str, List[LoadMomentEnvelopePoint]] = {
    "CRANE-001": [
        LoadMomentEnvelopePoint(distance=5.0, max_load=10.0),
        LoadMomentEnvelopePoint(distance=15.0, max_load=8.5),
        LoadMomentEnvelopePoint(distance=30.0, max_load=5.8),
        LoadMomentEnvelopePoint(distance=45.0, max_load=3.8),
        LoadMomentEnvelopePoint(distance=60.0, max_load=2.5),
    ],
    "CRANE-002": [
        LoadMomentEnvelopePoint(distance=5.0, max_load=8.0),
        LoadMomentEnvelopePoint(distance=14.0, max_load=7.0),
        LoadMomentEnvelopePoint(distance=28.0, max_load=4.8),
        LoadMomentEnvelopePoint(distance=42.0, max_load=3.2),
        LoadMomentEnvelopePoint(distance=55.0, max_load=2.0),
    ],
    "CRANE-003": [
        LoadMomentEnvelopePoint(distance=5.0, max_load=12.0),
        LoadMomentEnvelopePoint(distance=12.0, max_load=10.5),
        LoadMomentEnvelopePoint(distance=25.0, max_load=7.2),
        LoadMomentEnvelopePoint(distance=38.0, max_load=4.8),
        LoadMomentEnvelopePoint(distance=50.0, max_load=3.2),
    ],
}


def _datetime_str(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def init_load_moment_monitor_module():
    from collision import cranes_config as _cranes_config
    for crane_id in _cranes_config.keys():
        if crane_id not in cranes_envelope_curves:
            if crane_id in PRESET_ENVELOPES:
                cranes_envelope_curves[crane_id] = PRESET_ENVELOPES[crane_id]
            else:
                config = _cranes_config[crane_id]
                max_load = config.max_load
                arm_length = config.arm_length
                cranes_envelope_curves[crane_id] = [
                    LoadMomentEnvelopePoint(distance=5.0, max_load=max_load),
                    LoadMomentEnvelopePoint(distance=arm_length * 0.25, max_load=max_load * 0.85),
                    LoadMomentEnvelopePoint(distance=arm_length * 0.5, max_load=max_load * 0.60),
                    LoadMomentEnvelopePoint(distance=arm_length * 0.75, max_load=max_load * 0.40),
                    LoadMomentEnvelopePoint(distance=arm_length, max_load=max_load * 0.25),
                ]
        if crane_id not in cranes_weight_records:
            cranes_weight_records[crane_id] = deque(maxlen=MAX_WEIGHT_RECORDS_PER_CRANE)
    print(f"[称重监控模块] 初始化完成，已加载 {len(cranes_envelope_curves)} 台塔吊的力矩包络曲线")


def validate_envelope_points(points: List[LoadMomentEnvelopePoint]) -> Optional[str]:
    if len(points) < 5:
        return f"力矩包络曲线至少需要5个采样点，当前提供 {len(points)} 个"
    sorted_points = sorted(points, key=lambda p: p.distance)
    distances = [p.distance for p in sorted_points]
    for i in range(1, len(distances)):
        if distances[i] <= distances[i - 1]:
            return f"变幅距离必须严格递增，发现重复或逆序: {distances[i-1]} -> {distances[i]}"
    loads = [p.max_load for p in sorted_points]
    for i in range(1, len(loads)):
        if loads[i] > loads[i - 1]:
            return f"最大载荷应随距离增大而递减，发现异常: 距离{distances[i-1]}m载荷{loads[i-1]}吨 -> 距离{distances[i]}m载荷{loads[i]}吨"
    for p in sorted_points:
        if p.distance <= 0:
            return f"变幅距离必须为正数: {p.distance}"
        if p.max_load <= 0:
            return f"最大载荷必须为正数: {p.max_load}"
    return None


def set_envelope_curve(crane_id: str, points: List[LoadMomentEnvelopePoint]) -> bool:
    if crane_id not in cranes_config:
        raise ValueError(f"塔吊 {crane_id} 不存在")
    err = validate_envelope_points(points)
    if err:
        raise ValueError(err)
    sorted_points = sorted(points, key=lambda p: p.distance)
    cranes_envelope_curves[crane_id] = sorted_points
    print(f"[称重监控模块] 塔吊 {crane_id} 力矩包络曲线已热更新，共 {len(sorted_points)} 个采样点")
    return True


def get_envelope_curve(crane_id: str) -> Optional[List[LoadMomentEnvelopePoint]]:
    return cranes_envelope_curves.get(crane_id)


def _linear_interpolate(x: float, x0: float, y0: float, x1: float, y1: float) -> float:
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def calculate_allowed_load(crane_id: str, trolley_position: float) -> Optional[float]:
    envelope = cranes_envelope_curves.get(crane_id)
    if not envelope:
        return None
    if trolley_position <= 0:
        return None
    sorted_points = sorted(envelope, key=lambda p: p.distance)
    min_dist = sorted_points[0].distance
    max_dist = sorted_points[-1].distance
    if trolley_position <= min_dist:
        return sorted_points[0].max_load
    if trolley_position >= max_dist:
        return sorted_points[-1].max_load
    for i in range(len(sorted_points) - 1):
        p0 = sorted_points[i]
        p1 = sorted_points[i + 1]
        if p0.distance <= trolley_position <= p1.distance:
            return _linear_interpolate(
                trolley_position,
                p0.distance, p0.max_load,
                p1.distance, p1.max_load,
            )
    return None


def determine_alarm_level(overload_ratio: float) -> Optional[OverloadAlarmLevel]:
    if overload_ratio < 0.9:
        return None
    elif 0.9 <= overload_ratio < 1.0:
        return OverloadAlarmLevel.YELLOW
    elif 1.0 <= overload_ratio < 1.1:
        return OverloadAlarmLevel.ORANGE
    else:
        return OverloadAlarmLevel.RED


def _build_alarm_message(level: OverloadAlarmLevel, weight: float, allowed: float, ratio: float) -> str:
    if level == OverloadAlarmLevel.YELLOW:
        return f"黄色预警: 当前载荷 {weight:.2f} 吨接近允许值 {allowed:.2f} 吨，超载比例 {ratio*100:.1f}%"
    elif level == OverloadAlarmLevel.ORANGE:
        return f"橙色告警: 当前载荷 {weight:.2f} 吨超过允许值 {allowed:.2f} 吨，超载比例 {ratio*100:.1f}%，塔吊已锁定"
    else:
        return f"红色告警: 当前载荷 {weight:.2f} 吨严重超过允许值 {allowed:.2f} 吨，超载比例 {ratio*100:.1f}%，塔吊已锁定并推送紧急通知"


def _trigger_emergency_notification(alarm: OverloadAlarmEvent):
    print(f"[称重监控模块][紧急通知] 塔吊 {alarm.crane_id} 触发红色超载告警！超载比例 {alarm.overload_ratio*100:.1f}%")
    for cb in emergency_notification_callbacks:
        try:
            cb(alarm)
        except Exception as e:
            print(f"[称重监控模块] 紧急通知回调执行失败: {e}")


def register_emergency_notification_callback(callback):
    if callback not in emergency_notification_callbacks:
        emergency_notification_callbacks.append(callback)


def process_weight_report(report: WeightSensorReport) -> Dict:
    if report.crane_id not in cranes_config:
        raise ValueError(f"塔吊 {report.crane_id} 不存在")
    if report.weight < 0:
        raise ValueError("重量值不能为负数")

    now = time.time()
    crane_id = report.crane_id

    if crane_id not in cranes_weight_records:
        cranes_weight_records[crane_id] = deque(maxlen=MAX_WEIGHT_RECORDS_PER_CRANE)

    current_status = cranes_current_status.get(crane_id)
    trolley_position = current_status.trolley_position if current_status else None

    allowed_load = None
    overload_ratio = None
    if trolley_position is not None:
        allowed_load = calculate_allowed_load(crane_id, trolley_position)
        if allowed_load and allowed_load > 0:
            overload_ratio = report.weight / allowed_load

    weight_record = WeightRecord(
        crane_id=crane_id,
        weight=report.weight,
        sensor_timestamp=report.sensor_timestamp,
        received_at=now,
        datetime_str=_datetime_str(report.sensor_timestamp),
        trolley_position=trolley_position,
        allowed_load=allowed_load,
        overload_ratio=overload_ratio,
    )
    cranes_weight_records[crane_id].append(weight_record)

    result = {
        "recorded": True,
        "crane_id": crane_id,
        "weight": report.weight,
        "trolley_position": trolley_position,
        "allowed_load": allowed_load,
        "overload_ratio": round(overload_ratio, 4) if overload_ratio is not None else None,
        "alarm_triggered": False,
        "alarm_level": None,
        "crane_locked": False,
    }

    if overload_ratio is None or allowed_load is None or trolley_position is None:
        return result

    alarm_level = determine_alarm_level(overload_ratio)
    if alarm_level is None:
        return result

    alarm_id = f"OVL-{uuid.uuid4().hex[:12].upper()}"
    alarm = OverloadAlarmEvent(
        alarm_id=alarm_id,
        crane_id=crane_id,
        timestamp=now,
        datetime_str=_datetime_str(now),
        alarm_level=alarm_level,
        realtime_weight=report.weight,
        allowed_load=allowed_load,
        trolley_position=trolley_position,
        overload_ratio=round(overload_ratio, 4),
        message=_build_alarm_message(alarm_level, report.weight, allowed_load, overload_ratio),
        crane_locked=False,
        emergency_notification=False,
    )

    if alarm_level == OverloadAlarmLevel.YELLOW:
        pass
    elif alarm_level == OverloadAlarmLevel.ORANGE:
        lock_crane(crane_id, f"超载橙色告警: 超载比例 {overload_ratio*100:.1f}%")
        alarm.crane_locked = True
    elif alarm_level == OverloadAlarmLevel.RED:
        lock_crane(crane_id, f"超载红色告警: 超载比例 {overload_ratio*100:.1f}%")
        alarm.crane_locked = True
        alarm.emergency_notification = True
        _trigger_emergency_notification(alarm)

    cranes_overload_alarms.append(alarm)

    result["alarm_triggered"] = True
    result["alarm_level"] = alarm_level.value
    result["crane_locked"] = alarm.crane_locked
    result["alarm_id"] = alarm_id
    result["alarm_message"] = alarm.message
    result["emergency_notification"] = alarm.emergency_notification

    print(f"[称重监控模块] 塔吊 {crane_id} 超载告警[{alarm_level.value}]: 重量={report.weight:.2f}t, 允许={allowed_load:.2f}t, 比例={overload_ratio*100:.1f}%")

    return result


def get_weight_history(
    crane_id: str,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    limit: int = 300,
) -> List[WeightRecord]:
    if crane_id not in cranes_weight_records:
        return []
    records = list(cranes_weight_records[crane_id])
    if start_time is not None:
        records = [r for r in records if r.sensor_timestamp >= start_time]
    if end_time is not None:
        records = [r for r in records if r.sensor_timestamp <= end_time]
    return records[-limit:]


def get_overload_alarm_history(
    crane_id: Optional[str] = None,
    alarm_level: Optional[OverloadAlarmLevel] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    limit: int = 200,
) -> List[OverloadAlarmEvent]:
    result = cranes_overload_alarms
    if crane_id:
        result = [a for a in result if a.crane_id == crane_id]
    if alarm_level:
        result = [a for a in result if a.alarm_level == alarm_level]
    if start_time is not None:
        result = [a for a in result if a.timestamp >= start_time]
    if end_time is not None:
        result = [a for a in result if a.timestamp <= end_time]
    return result[-limit:]


def get_realtime_load_status(crane_id: str) -> Optional[RealtimeLoadStatus]:
    if crane_id not in cranes_config:
        return None

    current_status = cranes_current_status.get(crane_id)
    trolley_position = current_status.trolley_position if current_status else None

    latest_record: Optional[WeightRecord] = None
    records = cranes_weight_records.get(crane_id)
    if records and len(records) > 0:
        latest_record = records[-1]

    allowed_load = None
    overload_ratio = None
    if trolley_position is not None:
        allowed_load = calculate_allowed_load(crane_id, trolley_position)
        if latest_record and allowed_load and allowed_load > 0:
            overload_ratio = latest_record.weight / allowed_load

    alarm_level = None
    if overload_ratio is not None:
        alarm_level = determine_alarm_level(overload_ratio)

    lock = cranes_lock_status.get(crane_id)
    is_locked = lock.is_locked if lock else False

    return RealtimeLoadStatus(
        crane_id=crane_id,
        latest_weight=latest_record.weight if latest_record else None,
        latest_sensor_timestamp=latest_record.sensor_timestamp if latest_record else None,
        latest_datetime_str=latest_record.datetime_str if latest_record else None,
        current_trolley_position=trolley_position,
        allowed_load=round(allowed_load, 4) if allowed_load is not None else None,
        overload_ratio=round(overload_ratio, 4) if overload_ratio is not None else None,
        alarm_level=alarm_level,
        is_locked=is_locked,
    )


def get_all_realtime_load_statuses() -> List[RealtimeLoadStatus]:
    result = []
    for crane_id in cranes_config.keys():
        status = get_realtime_load_status(crane_id)
        if status:
            result.append(status)
    return result


def get_overload_stats() -> Dict:
    total_alarms = len(cranes_overload_alarms)
    yellow = sum(1 for a in cranes_overload_alarms if a.alarm_level == OverloadAlarmLevel.YELLOW)
    orange = sum(1 for a in cranes_overload_alarms if a.alarm_level == OverloadAlarmLevel.ORANGE)
    red = sum(1 for a in cranes_overload_alarms if a.alarm_level == OverloadAlarmLevel.RED)
    locked_due_to_overload = sum(1 for a in cranes_overload_alarms if a.crane_locked)
    return {
        "total_alarms": total_alarms,
        "yellow_count": yellow,
        "orange_count": orange,
        "red_count": red,
        "locked_count": locked_due_to_overload,
        "emergency_notifications": sum(1 for a in cranes_overload_alarms if a.emergency_notification),
    }
