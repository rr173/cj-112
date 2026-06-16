import math
import time
import uuid
import asyncio
from datetime import datetime
from collections import deque
from typing import Dict, List, Optional, Deque

from models import (
    CraneStatus,
    CraneStatusRecord,
    AnomalyDetectionConfig,
    SlidingWindowStats,
    AnomalyEvent,
    AlarmEvent,
    AlarmType,
    CraneFreezeStatus,
    WorkOrderStatus,
)
from collision import (
    cranes_config,
    alarm_history,
    lock_crane,
    normalize_angle,
)
from scheduler import work_orders


anomaly_config = AnomalyDetectionConfig()

cranes_sliding_window: Dict[str, Deque[CraneStatusRecord]] = {}
cranes_anomaly_events: Dict[str, List[AnomalyEvent]] = {}
cranes_freeze_status: Dict[str, CraneFreezeStatus] = {}
cranes_moment_over_start: Dict[str, Optional[float]] = {}
cranes_consecutive_overspeed: Dict[str, int] = {}


def init_anomaly_detector():
    for crane_id in cranes_config.keys():
        if crane_id not in cranes_sliding_window:
            cranes_sliding_window[crane_id] = deque(maxlen=anomaly_config.sliding_window_size)
        if crane_id not in cranes_anomaly_events:
            cranes_anomaly_events[crane_id] = []
        if crane_id not in cranes_freeze_status:
            cranes_freeze_status[crane_id] = CraneFreezeStatus(
                crane_id=crane_id,
                is_frozen=False
            )
        if crane_id not in cranes_moment_over_start:
            cranes_moment_over_start[crane_id] = None
        if crane_id not in cranes_consecutive_overspeed:
            cranes_consecutive_overspeed[crane_id] = 0


def update_anomaly_config(new_config: AnomalyDetectionConfig):
    old_window_size = anomaly_config.sliding_window_size

    anomaly_config.sliding_window_size = new_config.sliding_window_size
    anomaly_config.rotation_reversal_threshold = new_config.rotation_reversal_threshold
    anomaly_config.max_trolley_speed = new_config.max_trolley_speed
    anomaly_config.trolley_overspeed_count = new_config.trolley_overspeed_count
    anomaly_config.load_moment_ratio_threshold = new_config.load_moment_ratio_threshold
    anomaly_config.load_moment_duration_threshold = new_config.load_moment_duration_threshold
    anomaly_config.rotation_freeze_duration = new_config.rotation_freeze_duration

    if new_config.sliding_window_size != old_window_size:
        for crane_id in cranes_sliding_window.keys():
            old_window = cranes_sliding_window[crane_id]
            new_window = deque(maxlen=new_config.sliding_window_size)
            for item in list(old_window)[-new_config.sliding_window_size:]:
                new_window.append(item)
            cranes_sliding_window[crane_id] = new_window

    refresh_all_freeze_status()


def get_anomaly_config() -> AnomalyDetectionConfig:
    return anomaly_config


def refresh_all_freeze_status():
    now = time.time()
    for crane_id in list(cranes_freeze_status.keys()):
        freeze = cranes_freeze_status[crane_id]
        if freeze.is_frozen and freeze.unfreeze_at and now >= freeze.unfreeze_at:
            freeze.is_frozen = False
            freeze.frozen_at = None
            freeze.frozen_reason = None
            freeze.unfreeze_at = None
            unfreeze_crane_record(crane_id, now)


def ensure_crane_initialized(crane_id: str):
    if crane_id not in cranes_sliding_window:
        cranes_sliding_window[crane_id] = deque(maxlen=anomaly_config.sliding_window_size)
    if crane_id not in cranes_anomaly_events:
        cranes_anomaly_events[crane_id] = []
    if crane_id not in cranes_freeze_status:
        cranes_freeze_status[crane_id] = CraneFreezeStatus(
            crane_id=crane_id,
            is_frozen=False
        )
    if crane_id not in cranes_moment_over_start:
        cranes_moment_over_start[crane_id] = None
    if crane_id not in cranes_consecutive_overspeed:
        cranes_consecutive_overspeed[crane_id] = 0


def is_crane_frozen(crane_id: str) -> bool:
    freeze = cranes_freeze_status.get(crane_id)
    if not freeze or not freeze.is_frozen:
        return False
    if freeze.unfreeze_at and time.time() >= freeze.unfreeze_at:
        freeze.is_frozen = False
        freeze.frozen_at = None
        freeze.frozen_reason = None
        freeze.unfreeze_at = None
        return False
    return True


def freeze_crane(crane_id: str, reason: str, duration: float):
    ensure_crane_initialized(crane_id)
    now = time.time()
    cranes_freeze_status[crane_id] = CraneFreezeStatus(
        crane_id=crane_id,
        is_frozen=True,
        frozen_at=now,
        frozen_reason=reason,
        unfreeze_at=now + duration
    )
    try:
        from daily_report import add_freeze_lock_record
        add_freeze_lock_record(crane_id, "FREEZE", "START", now, reason, now + duration)
    except ImportError:
        pass


def unfreeze_crane_record(crane_id: str, timestamp: float):
    try:
        from daily_report import add_freeze_lock_record
        add_freeze_lock_record(crane_id, "FREEZE", "END", timestamp)
    except ImportError:
        pass


def add_status_to_window(status: CraneStatus):
    ensure_crane_initialized(status.crane_id)
    record = CraneStatusRecord(
        crane_id=status.crane_id,
        rotation_angle=status.rotation_angle,
        trolley_position=status.trolley_position,
        hook_height=status.hook_height,
        timestamp=status.timestamp or time.time()
    )
    cranes_sliding_window[status.crane_id].append(record)


def get_current_executing_order_weight(crane_id: str) -> float:
    for order in work_orders.values():
        if (order.status == WorkOrderStatus.EXECUTING and
                order.assigned_crane_id == crane_id):
            return order.weight
    return 0.0


def calculate_moment(crane_id: str, trolley_position: float) -> tuple:
    config = cranes_config.get(crane_id)
    if not config:
        return 0.0, 0.0, 0.0
    current_weight = get_current_executing_order_weight(crane_id)
    current_moment = trolley_position * current_weight
    max_moment = config.arm_length * config.max_load * anomaly_config.load_moment_ratio_threshold
    ratio = current_moment / max_moment if max_moment > 0 else 0.0
    return current_moment, max_moment, ratio


def create_alarm_event(
    crane_id: str,
    alarm_type: AlarmType,
    message: str,
    details: Dict
) -> AlarmEvent:
    now = time.time()
    return AlarmEvent(
        alarm_id=str(uuid.uuid4()),
        alarm_type=alarm_type,
        timestamp=now,
        datetime_str=datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        crane_a_id=crane_id,
        crane_b_id=crane_id,
        message=message,
        details=details
    )


def create_anomaly_event(
    crane_id: str,
    alarm_type: AlarmType,
    message: str,
    details: Dict
) -> AnomalyEvent:
    now = time.time()
    event = AnomalyEvent(
        event_id=str(uuid.uuid4()),
        alarm_type=alarm_type,
        timestamp=now,
        datetime_str=datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        crane_id=crane_id,
        message=message,
        details=details
    )
    ensure_crane_initialized(crane_id)
    cranes_anomaly_events[crane_id].append(event)
    return event


def detect_rotation_oscillation(crane_id: str) -> Optional[AlarmEvent]:
    window = cranes_sliding_window.get(crane_id)
    if not window or len(window) < 2:
        return None

    records = list(window)
    now = time.time()
    one_minute_ago = now - 60.0
    recent_records = [r for r in records if r.timestamp >= one_minute_ago]

    if len(recent_records) < 2:
        return None

    reversal_count = 0
    prev_direction = None

    for i in range(1, len(recent_records)):
        prev_angle = normalize_angle(recent_records[i - 1].rotation_angle)
        curr_angle = normalize_angle(recent_records[i].rotation_angle)

        delta = curr_angle - prev_angle
        if delta > 180:
            delta -= 360
        elif delta < -180:
            delta += 360

        if abs(delta) < 0.1:
            continue

        direction = 1 if delta > 0 else -1

        if prev_direction is not None and direction != prev_direction:
            reversal_count += 1

        prev_direction = direction

    if reversal_count >= anomaly_config.rotation_reversal_threshold:
        details = {
            "reversal_count": reversal_count,
            "threshold": anomaly_config.rotation_reversal_threshold,
            "window_records": len(recent_records),
            "time_window_seconds": 60
        }
        message = (
            f"回转震荡检测告警: 1分钟内回转方向反转{reversal_count}次, "
            f"超过阈值{anomaly_config.rotation_reversal_threshold}次"
        )
        alarm = create_alarm_event(crane_id, AlarmType.ROTATION_OSCILLATION, message, details)
        alarm_history.append(alarm)
        create_anomaly_event(crane_id, AlarmType.ROTATION_OSCILLATION, message, details)

        freeze_crane(
            crane_id,
            f"回转震荡告警自动冻结: {reversal_count}次反转",
            anomaly_config.rotation_freeze_duration
        )
        return alarm

    return None


def detect_trolley_overspeed(crane_id: str) -> Optional[AlarmEvent]:
    window = cranes_sliding_window.get(crane_id)
    if not window or len(window) < 2:
        cranes_consecutive_overspeed[crane_id] = 0
        return None

    records = list(window)
    last = records[-1]
    prev = records[-2]

    time_diff = last.timestamp - prev.timestamp
    if time_diff <= 0:
        return None

    position_diff = abs(last.trolley_position - prev.trolley_position)
    speed = position_diff / time_diff

    if speed > anomaly_config.max_trolley_speed:
        cranes_consecutive_overspeed[crane_id] += 1
    else:
        cranes_consecutive_overspeed[crane_id] = 0
        return None

    consecutive = cranes_consecutive_overspeed[crane_id]
    if consecutive >= anomaly_config.trolley_overspeed_count:
        details = {
            "current_speed": round(speed, 3),
            "max_speed": anomaly_config.max_trolley_speed,
            "consecutive_count": consecutive,
            "threshold_count": anomaly_config.trolley_overspeed_count,
            "time_diff": round(time_diff, 3),
            "position_diff": round(position_diff, 3)
        }
        message = (
            f"变幅超速检测告警: 连续{consecutive}次变幅速度{speed:.3f}m/s "
            f"超过最大限制{anomaly_config.max_trolley_speed}m/s"
        )
        alarm = create_alarm_event(crane_id, AlarmType.TROLLEY_OVERSPEED, message, details)
        alarm_history.append(alarm)
        create_anomaly_event(crane_id, AlarmType.TROLLEY_OVERSPEED, message, details)

        cranes_consecutive_overspeed[crane_id] = 0
        return alarm

    return None


def detect_load_moment(crane_id: str) -> Optional[AlarmEvent]:
    window = cranes_sliding_window.get(crane_id)
    if not window or len(window) < 1:
        cranes_moment_over_start[crane_id] = None
        return None

    last_record = window[-1]
    current_moment, max_moment, ratio = calculate_moment(
        crane_id, last_record.trolley_position
    )

    config = cranes_config.get(crane_id)
    if not config:
        return None

    theoretical_max_moment = config.arm_length * config.max_load

    if ratio >= 1.0:
        if cranes_moment_over_start[crane_id] is None:
            cranes_moment_over_start[crane_id] = last_record.timestamp
        else:
            duration = last_record.timestamp - cranes_moment_over_start[crane_id]
            if duration >= anomaly_config.load_moment_duration_threshold:
                details = {
                    "current_moment": round(current_moment, 3),
                    "max_allowed_moment": round(max_moment, 3),
                    "theoretical_max_moment": round(theoretical_max_moment, 3),
                    "moment_ratio": round(ratio, 3),
                    "over_duration": round(duration, 3),
                    "threshold_duration": anomaly_config.load_moment_duration_threshold,
                    "trolley_position": last_record.trolley_position,
                    "current_weight": get_current_executing_order_weight(crane_id)
                }
                message = (
                    f"载荷力矩预警: 力矩{current_moment:.2f}吨·米超过允许值"
                    f"{max_moment:.2f}吨·米(理论最大{theoretical_max_moment:.2f}吨·米的70%), "
                    f"已持续{duration:.1f}秒"
                )
                alarm = create_alarm_event(crane_id, AlarmType.LOAD_MOMENT_WARNING, message, details)
                alarm_history.append(alarm)
                create_anomaly_event(crane_id, AlarmType.LOAD_MOMENT_WARNING, message, details)

                lock_crane(crane_id, f"力矩超限自动锁定: {current_moment:.2f}吨·米")

                cranes_moment_over_start[crane_id] = None
                return alarm
    else:
        cranes_moment_over_start[crane_id] = None

    return None


def detect_anomalies(crane_id: str) -> List[AlarmEvent]:
    triggered_alarms: List[AlarmEvent] = []

    oscillation_alarm = detect_rotation_oscillation(crane_id)
    if oscillation_alarm:
        triggered_alarms.append(oscillation_alarm)

    overspeed_alarm = detect_trolley_overspeed(crane_id)
    if overspeed_alarm:
        triggered_alarms.append(overspeed_alarm)

    moment_alarm = detect_load_moment(crane_id)
    if moment_alarm:
        triggered_alarms.append(moment_alarm)

    return triggered_alarms


def get_sliding_window_stats(crane_id: str) -> Optional[SlidingWindowStats]:
    ensure_crane_initialized(crane_id)
    window = cranes_sliding_window.get(crane_id)
    if not window or len(window) == 0:
        return None

    records = list(window)
    config = cranes_config.get(crane_id)
    if not config:
        return None

    total_rotation_delta = 0.0
    total_trolley_delta = 0.0
    total_time = 0.0
    rotation_reversal_count = 0
    prev_direction = None

    for i in range(1, len(records)):
        time_diff = records[i].timestamp - records[i - 1].timestamp
        if time_diff <= 0:
            continue

        prev_angle = normalize_angle(records[i - 1].rotation_angle)
        curr_angle = normalize_angle(records[i].rotation_angle)
        rotation_delta = curr_angle - prev_angle
        if rotation_delta > 180:
            rotation_delta -= 360
        elif rotation_delta < -180:
            rotation_delta += 360

        direction = 1 if rotation_delta > 0 else -1 if rotation_delta < 0 else prev_direction
        if prev_direction is not None and direction is not None and direction != prev_direction:
            rotation_reversal_count += 1
        prev_direction = direction

        total_rotation_delta += abs(rotation_delta)
        total_trolley_delta += abs(records[i].trolley_position - records[i - 1].trolley_position)
        total_time += time_diff

    avg_rotation_speed = total_rotation_delta / total_time if total_time > 0 else 0.0
    avg_trolley_speed = total_trolley_delta / total_time if total_time > 0 else 0.0

    last_record = records[-1]
    current_moment, max_moment, ratio = calculate_moment(crane_id, last_record.trolley_position)

    alarm_count = sum(
        1 for a in alarm_history
        if (a.crane_a_id == crane_id or a.crane_b_id == crane_id) and
        a.timestamp >= records[0].timestamp
    )

    return SlidingWindowStats(
        crane_id=crane_id,
        window_size=anomaly_config.sliding_window_size,
        current_count=len(records),
        avg_rotation_speed=round(avg_rotation_speed, 4),
        avg_trolley_speed=round(avg_trolley_speed, 4),
        current_moment=round(current_moment, 3),
        max_moment=round(max_moment, 3),
        moment_ratio=round(ratio, 4),
        alarm_count_in_window=alarm_count,
        first_timestamp=records[0].timestamp,
        last_timestamp=records[-1].timestamp,
        rotation_reversal_count=rotation_reversal_count,
        trolley_overspeed_count=cranes_consecutive_overspeed.get(crane_id, 0)
    )


def get_anomaly_events(crane_id: str, limit: int = 100) -> List[AnomalyEvent]:
    ensure_crane_initialized(crane_id)
    events = cranes_anomaly_events.get(crane_id, [])
    return events[-limit:]


def get_all_anomaly_events(limit: int = 100) -> List[AnomalyEvent]:
    all_events = []
    for crane_id in cranes_anomaly_events.keys():
        all_events.extend(cranes_anomaly_events[crane_id])
    all_events.sort(key=lambda e: e.timestamp)
    return all_events[-limit:]


def process_status_report(status: CraneStatus):
    if is_crane_frozen(status.crane_id):
        return

    add_status_to_window(status)
    detect_anomalies(status.crane_id)


async def process_status_report_async(status: CraneStatus):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, process_status_report, status)
