import math
import time
import uuid
from typing import Dict, List, Optional, Tuple

from models import (
    TowerCraneConfig,
    CraneStatus,
    CoordinateSnapshot,
    AlarmEvent,
    LockStatus,
    AlarmType,
)

DEFAULT_SAFETY_THRESHOLD = 5.0

cranes_config: Dict[str, TowerCraneConfig] = {}
cranes_current_status: Dict[str, CraneStatus] = {}
cranes_lock_status: Dict[str, LockStatus] = {}
alarm_history: List[AlarmEvent] = []
pair_safety_thresholds: Dict[str, float] = {}


def get_pair_key(a: str, b: str) -> str:
    return "|".join(sorted([a, b]))


def get_safety_threshold(a: str, b: str) -> float:
    return pair_safety_thresholds.get(get_pair_key(a, b), DEFAULT_SAFETY_THRESHOLD)


def normalize_angle(angle: float) -> float:
    angle = angle % 360.0
    if angle < 0:
        angle += 360.0
    return angle


def angle_in_interval(angle: float, interval) -> bool:
    from models import AngleInterval
    angle = normalize_angle(angle)
    if not interval.wraps_zero:
        return interval.start <= angle <= interval.end
    else:
        return angle >= interval.start or angle <= interval.end


def make_angle_interval(start_deg: float, end_deg: float):
    from models import AngleInterval
    s = normalize_angle(start_deg)
    e = normalize_angle(end_deg)
    wraps = s > e
    return AngleInterval(start=s, end=e, wraps_zero=wraps)


def polar_to_cartesian(tower_x: float, tower_y: float, tower_z: float,
                       angle_deg: float, radius: float) -> Dict[str, float]:
    angle_rad = math.radians(angle_deg)
    x = tower_x + radius * math.cos(angle_rad)
    y = tower_y + radius * math.sin(angle_rad)
    z = tower_z
    return {"x": x, "y": y, "z": z}


def euclidean_distance_3d(p1: Dict[str, float], p2: Dict[str, float]) -> float:
    return math.sqrt(
        (p1["x"] - p2["x"]) ** 2 +
        (p1["y"] - p2["y"]) ** 2 +
        (p1["z"] - p2["z"]) ** 2
    )


def compute_crane_coords(crane_id: str, status: CraneStatus) -> Optional[CoordinateSnapshot]:
    config = cranes_config.get(crane_id)
    if not config:
        return None

    arm_end = polar_to_cartesian(
        config.tower_x, config.tower_y, config.tower_z,
        status.rotation_angle, status.trolley_position
    )

    rope_length = max(0.0, config.tower_z - status.hook_height)
    swing_radius = rope_length * 0.10

    hook = polar_to_cartesian(
        config.tower_x, config.tower_y, status.hook_height,
        status.rotation_angle, status.trolley_position
    )

    return CoordinateSnapshot(
        crane_id=crane_id,
        arm_end_x=arm_end["x"],
        arm_end_y=arm_end["y"],
        arm_end_z=arm_end["z"],
        hook_x=hook["x"],
        hook_y=hook["y"],
        hook_z=hook["z"],
        swing_radius=swing_radius,
        rotation_angle=status.rotation_angle,
        trolley_position=status.trolley_position,
        hook_height=status.hook_height,
    )


def check_collision_between(a_id: str, b_id: str,
                            snap_a: CoordinateSnapshot,
                            snap_b: CoordinateSnapshot) -> Optional[AlarmEvent]:
    from datetime import datetime
    threshold = get_safety_threshold(a_id, b_id)

    arm_end_a = {"x": snap_a.arm_end_x, "y": snap_a.arm_end_y, "z": snap_a.arm_end_z}
    arm_end_b = {"x": snap_b.arm_end_x, "y": snap_b.arm_end_y, "z": snap_b.arm_end_z}
    dist_arm = euclidean_distance_3d(arm_end_a, arm_end_b)

    hook_a = {"x": snap_a.hook_x, "y": snap_a.hook_y, "z": snap_a.hook_z}
    hook_b = {"x": snap_b.hook_x, "y": snap_b.hook_y, "z": snap_b.hook_z}
    dist_hook = euclidean_distance_3d(hook_a, hook_b)

    swing_sum = snap_a.swing_radius + snap_b.swing_radius
    effective_threshold = threshold + swing_sum

    min_dist = min(dist_arm, dist_hook)

    if min_dist < effective_threshold:
        now = time.time()
        return AlarmEvent(
            alarm_id=str(uuid.uuid4()),
            alarm_type=AlarmType.COLLISION,
            timestamp=now,
            datetime_str=datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
            crane_a_id=a_id,
            crane_b_id=b_id,
            distance=min_dist,
            safety_threshold=effective_threshold,
            crane_a_snapshot=snap_a,
            crane_b_snapshot=snap_b,
            message=(
                f"碰撞风险: 塔吊[{a_id}]与[{b_id}]最小距离{min_dist:.2f}米 "
                f"< 安全阈值{effective_threshold:.2f}米 "
                f"(基础阈值{threshold}米 + 吊钩摆幅{swing_sum:.2f}米)"
            )
        )
    return None


def lock_crane(crane_id: str, reason: str):
    if crane_id in cranes_lock_status:
        cranes_lock_status[crane_id].is_locked = True
        cranes_lock_status[crane_id].locked_at = time.time()
        cranes_lock_status[crane_id].locked_reason = reason
        try:
            from daily_report import add_freeze_lock_record
            add_freeze_lock_record(crane_id, "LOCK", "START", time.time(), reason)
        except ImportError:
            pass


def unlock_crane_record(crane_id: str):
    if crane_id in cranes_lock_status:
        try:
            from daily_report import add_freeze_lock_record
            add_freeze_lock_record(crane_id, "LOCK", "END", time.time())
        except ImportError:
            pass


def compute_bearing(from_x: float, from_y: float, to_x: float, to_y: float) -> float:
    dx = to_x - from_x
    dy = to_y - from_y
    bearing = math.degrees(math.atan2(dy, dx))
    return normalize_angle(bearing)


def can_crane_reach_point(crane_id: str, x: float, y: float) -> bool:
    config = cranes_config.get(crane_id)
    if not config:
        return False
    dx = x - config.tower_x
    dy = y - config.tower_y
    distance = math.sqrt(dx * dx + dy * dy)
    if distance > config.arm_length:
        return False
    bearing = math.degrees(math.atan2(dy, dx))
    bearing = normalize_angle(bearing)
    if bearing < config.min_angle or bearing > config.max_angle:
        return False
    return True
