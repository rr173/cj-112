import math
import time
import uuid
from typing import Dict, List, Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="塔吊防碰撞联锁服务", description="建筑工地多塔吊防碰撞实时监测系统")


class TowerCraneConfig(BaseModel):
    crane_id: str
    name: str
    tower_x: float = Field(description="塔身X坐标(米)")
    tower_y: float = Field(description="塔身Y坐标(米)")
    tower_z: float = Field(description="塔身顶部高度(米)")
    arm_length: float = Field(description="臂长(米)")
    max_load: float = Field(description="最大起重量(吨)")
    min_angle: float = Field(default=0.0, description="最小回转角度(度)")
    max_angle: float = Field(default=360.0, description="最大回转角度(度)")


class CraneStatus(BaseModel):
    crane_id: str
    rotation_angle: float = Field(description="回转角度(度), 0-360")
    trolley_position: float = Field(description="变幅小车位置(米), 0到臂长")
    hook_height: float = Field(description="吊钩高度(米, 从地面算起)")
    timestamp: Optional[float] = None


class CoordinateSnapshot(BaseModel):
    crane_id: str
    arm_end_x: float
    arm_end_y: float
    arm_end_z: float
    hook_x: float
    hook_y: float
    hook_z: float
    swing_radius: float
    rotation_angle: float
    trolley_position: float
    hook_height: float


class AlarmEvent(BaseModel):
    alarm_id: str
    timestamp: float
    datetime_str: str
    crane_a_id: str
    crane_b_id: str
    distance: float
    safety_threshold: float
    crane_a_snapshot: CoordinateSnapshot
    crane_b_snapshot: CoordinateSnapshot
    message: str


class LockStatus(BaseModel):
    crane_id: str
    is_locked: bool
    locked_at: Optional[float] = None
    locked_reason: Optional[str] = None


class CraneFullStatus(BaseModel):
    config: TowerCraneConfig
    current_status: Optional[CraneStatus] = None
    lock_status: LockStatus
    arm_end_coords: Optional[Dict[str, float]] = None
    hook_coords: Optional[Dict[str, float]] = None
    swing_radius: Optional[float] = None


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

    crane_ids = list(cranes_config.keys())
    for i in range(len(crane_ids)):
        for j in range(i + 1, len(crane_ids)):
            pair_safety_thresholds[get_pair_key(crane_ids[i], crane_ids[j])] = DEFAULT_SAFETY_THRESHOLD


@app.post("/api/crane/status", summary="上报塔吊状态(每秒一次)")
def report_crane_status(status: CraneStatus):
    if status.crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {status.crane_id} 不存在")

    lock = cranes_lock_status.get(status.crane_id)
    if lock and lock.is_locked:
        raise HTTPException(
            status_code=423,
            detail={
                "message": "塔吊已锁定,拒绝状态上报",
                "crane_id": status.crane_id,
                "locked_at": lock.locked_at,
                "locked_reason": lock.locked_reason,
            }
        )

    config = cranes_config[status.crane_id]
    if not (config.min_angle <= status.rotation_angle <= config.max_angle):
        raise HTTPException(
            status_code=400,
            detail=f"回转角度超出限制范围 [{config.min_angle}, {config.max_angle}]"
        )
    if not (0 <= status.trolley_position <= config.arm_length):
        raise HTTPException(
            status_code=400,
            detail=f"变幅小车位置超出范围 [0, {config.arm_length}]"
        )
    if status.hook_height < 0:
        raise HTTPException(status_code=400, detail="吊钩高度不能为负数")

    status.timestamp = status.timestamp or time.time()
    cranes_current_status[status.crane_id] = status

    triggered_alarms: List[AlarmEvent] = []
    other_ids = [cid for cid in cranes_current_status.keys() if cid != status.crane_id]

    snap_current = compute_crane_coords(status.crane_id, status)

    for other_id in other_ids:
        other_status = cranes_current_status[other_id]
        snap_other = compute_crane_coords(other_id, other_status)
        if snap_current and snap_other:
            alarm = check_collision_between(status.crane_id, other_id, snap_current, snap_other)
            if alarm:
                triggered_alarms.append(alarm)
                alarm_history.append(alarm)
                lock_crane(status.crane_id, f"与塔吊{other_id}距离过近触发防碰撞锁定")
                lock_crane(other_id, f"与塔吊{status.crane_id}距离过近触发防碰撞锁定")

    return {
        "code": 0,
        "message": "状态上报成功",
        "crane_id": status.crane_id,
        "locked": cranes_lock_status[status.crane_id].is_locked if status.crane_id in cranes_lock_status else False,
        "alarms_triggered": len(triggered_alarms),
        "alarm_details": triggered_alarms,
    }


@app.post("/api/crane/{crane_id}/unlock", summary="人工解除塔吊锁定")
def unlock_crane(crane_id: str):
    if crane_id not in cranes_lock_status:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    lock = cranes_lock_status[crane_id]
    was_locked = lock.is_locked
    lock.is_locked = False
    prev_reason = lock.locked_reason
    lock.locked_reason = None
    lock.locked_at = None
    return {
        "code": 0,
        "message": "锁定已解除" if was_locked else "塔吊本来就处于未锁定状态",
        "crane_id": crane_id,
        "previous_lock_reason": prev_reason,
    }


@app.get("/api/crane/{crane_id}/status", summary="查询塔吊实时状态")
def get_crane_full_status(crane_id: str):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    config = cranes_config[crane_id]
    status = cranes_current_status.get(crane_id)
    lock = cranes_lock_status[crane_id]

    arm_end = None
    hook = None
    swing = None
    if status:
        snap = compute_crane_coords(crane_id, status)
        if snap:
            arm_end = {"x": snap.arm_end_x, "y": snap.arm_end_y, "z": snap.arm_end_z}
            hook = {"x": snap.hook_x, "y": snap.hook_y, "z": snap.hook_z}
            swing = snap.swing_radius

    return CraneFullStatus(
        config=config,
        current_status=status,
        lock_status=lock,
        arm_end_coords=arm_end,
        hook_coords=hook,
        swing_radius=swing,
    )


@app.get("/api/cranes", summary="查询所有塔吊列表及状态")
def get_all_cranes():
    result = []
    for cid in cranes_config.keys():
        try:
            result.append(get_crane_full_status(cid))
        except Exception:
            pass
    return result


@app.get("/api/alarms", summary="查询告警历史")
def get_alarm_history(crane_id: Optional[str] = None, limit: int = 100):
    result = alarm_history
    if crane_id:
        result = [a for a in result if a.crane_a_id == crane_id or a.crane_b_id == crane_id]
    return result[-limit:]


@app.get("/api/locks", summary="查询所有塔吊当前锁定状态")
def get_all_lock_status():
    return list(cranes_lock_status.values())


@app.get("/api/thresholds", summary="查询各塔吊对之间的安全间距阈值")
def get_thresholds():
    result = []
    for pair_key, threshold in pair_safety_thresholds.items():
        a, b = pair_key.split("|")
        result.append({
            "crane_a": a,
            "crane_b": b,
            "safety_threshold_meters": threshold,
            "default_threshold_meters": DEFAULT_SAFETY_THRESHOLD,
        })
    return result


@app.get("/health", summary="健康检查")
def health_check():
    return {
        "status": "ok",
        "service": "塔吊防碰撞联锁服务",
        "cranes_registered": len(cranes_config),
        "total_alarms": len(alarm_history),
        "locked_cranes": sum(1 for l in cranes_lock_status.values() if l.is_locked),
        "timestamp": time.time(),
    }
