import math
import time
import uuid
from typing import Dict, List, Optional, Tuple, Deque
from collections import deque
from datetime import datetime
from enum import Enum

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="塔吊防碰撞联锁服务", description="建筑工地多塔吊防碰撞实时监测系统")


DEFAULT_TOKEN_WAIT_TIMEOUT = 30.0
DEFAULT_TOKEN_MAX_HOLD_TIME = 120.0


class ZoneArbConfig(BaseModel):
    token_wait_timeout: float = Field(default=DEFAULT_TOKEN_WAIT_TIMEOUT, description="令牌申请等待超时时间(秒)")
    token_max_hold_time: float = Field(default=DEFAULT_TOKEN_MAX_HOLD_TIME, description="令牌最大占用时长(秒)")


zone_arb_config = ZoneArbConfig()


class AngleInterval(BaseModel):
    start: float = Field(description="起始角度(度, 0-360), 区间为闭区间[start, end]")
    end: float = Field(description="结束角度(度, 0-360)")
    wraps_zero: bool = Field(default=False, description="是否跨越0度(360度)")


class OverlapSector(BaseModel):
    sector_id: str
    crane_a_id: str
    crane_b_id: str
    crane_a_interval: AngleInterval
    crane_b_interval: AngleInterval
    distance_between_towers: float
    created_at: float


class WaitQueueItem(BaseModel):
    crane_id: str
    request_id: str
    requested_at: float


class TokenStatus(BaseModel):
    sector_id: str
    holder_crane_id: Optional[str] = None
    acquired_at: Optional[float] = None
    expires_at: Optional[float] = None
    wait_queue: List[WaitQueueItem] = []


class TokenRequest(BaseModel):
    crane_id: str
    sector_id: str


class TokenReleaseRequest(BaseModel):
    crane_id: str
    sector_id: str
    request_id: Optional[str] = None


class EventType(str, Enum):
    TOKEN_ACQUIRED = "TOKEN_ACQUIRED"
    TOKEN_RELEASED = "TOKEN_RELEASED"
    TOKEN_REVOKED = "TOKEN_REVOKED"
    TOKEN_REQUEST_TIMEOUT = "TOKEN_REQUEST_TIMEOUT"
    TOKEN_ENQUEUED = "TOKEN_ENQUEUED"
    TOKEN_DEQUEUED = "TOKEN_DEQUEUED"
    STATUS_REJECTED_NO_TOKEN = "STATUS_REJECTED_NO_TOKEN"
    STATUS_REJECTED_STILL_IN_ZONE = "STATUS_REJECTED_STILL_IN_ZONE"
    SECTOR_DETECTED = "SECTOR_DETECTED"
    SECTOR_REMOVED = "SECTOR_REMOVED"


class ArbEventLog(BaseModel):
    event_id: str
    event_type: EventType
    timestamp: float
    datetime_str: str
    crane_id: Optional[str] = None
    sector_id: Optional[str] = None
    details: Dict = {}


class CraneTokensView(BaseModel):
    crane_id: str
    held_tokens: List[Dict] = []
    pending_requests: List[Dict] = []


class CraneTokenRejectionInfo(BaseModel):
    crane_id: str
    sector_id: str
    last_rejected_at: float


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

overlap_sectors: Dict[str, OverlapSector] = {}
sector_pair_index: Dict[str, str] = {}
token_statuses: Dict[str, TokenStatus] = {}
arb_event_logs: List[ArbEventLog] = []
cranes_held_tokens: Dict[str, set] = {}
cranes_pending_requests: Dict[str, Dict[str, str]] = {}
cranes_token_rejection: Dict[str, CraneTokenRejectionInfo] = {}


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


def normalize_angle(angle: float) -> float:
    angle = angle % 360.0
    if angle < 0:
        angle += 360.0
    return angle


def angle_in_interval(angle: float, interval: AngleInterval) -> bool:
    angle = normalize_angle(angle)
    if not interval.wraps_zero:
        return interval.start <= angle <= interval.end
    else:
        return angle >= interval.start or angle <= interval.end


def make_angle_interval(start_deg: float, end_deg: float) -> AngleInterval:
    s = normalize_angle(start_deg)
    e = normalize_angle(end_deg)
    wraps = s > e
    return AngleInterval(start=s, end=e, wraps_zero=wraps)


def log_arb_event(event_type: EventType, crane_id: Optional[str] = None,
                  sector_id: Optional[str] = None, details: Optional[Dict] = None):
    now = time.time()
    event = ArbEventLog(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        timestamp=now,
        datetime_str=datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        crane_id=crane_id,
        sector_id=sector_id,
        details=details or {},
    )
    arb_event_logs.append(event)


def compute_bearing(from_x: float, from_y: float, to_x: float, to_y: float) -> float:
    dx = to_x - from_x
    dy = to_y - from_y
    bearing = math.degrees(math.atan2(dy, dx))
    return normalize_angle(bearing)


def compute_overlap_interval(bearing_to_other_deg: float,
                             self_arm: float,
                             other_arm: float,
                             distance: float) -> Optional[AngleInterval]:
    if distance >= self_arm + other_arm:
        return None
    if distance + min(self_arm, other_arm) <= max(self_arm, other_arm):
        inner = max(self_arm, other_arm) - distance
        outer = min(self_arm, other_arm) + distance
        half_span = math.degrees(math.acos(max(-1.0, min(1.0, inner / self_arm)))) if self_arm > 0 else 0
        return make_angle_interval(bearing_to_other_deg - half_span, bearing_to_other_deg + half_span)
    cos_alpha = (self_arm ** 2 + distance ** 2 - other_arm ** 2) / (2 * self_arm * distance)
    cos_alpha = max(-1.0, min(1.0, cos_alpha))
    half_span = math.degrees(math.acos(cos_alpha))
    return make_angle_interval(bearing_to_other_deg - half_span, bearing_to_other_deg + half_span)


def detect_overlap_sectors_for_pair(a_id: str, b_id: str) -> Optional[OverlapSector]:
    a = cranes_config.get(a_id)
    b = cranes_config.get(b_id)
    if not a or not b:
        return None

    dx = b.tower_x - a.tower_x
    dy = b.tower_y - a.tower_y
    dist = math.sqrt(dx * dx + dy * dy)

    interval_a = compute_overlap_interval(
        bearing_to_other_deg=compute_bearing(a.tower_x, a.tower_y, b.tower_x, b.tower_y),
        self_arm=a.arm_length,
        other_arm=b.arm_length,
        distance=dist,
    )
    if interval_a is None:
        return None

    interval_b = compute_overlap_interval(
        bearing_to_other_deg=compute_bearing(b.tower_x, b.tower_y, a.tower_x, a.tower_y),
        self_arm=b.arm_length,
        other_arm=a.arm_length,
        distance=dist,
    )

    sorted_ids = sorted([a_id, b_id])
    sector_id = f"SEC-{get_pair_key(a_id, b_id)}"
    crane_a_id_sorted = sorted_ids[0]
    crane_b_id_sorted = sorted_ids[1]
    sector = OverlapSector(
        sector_id=sector_id,
        crane_a_id=crane_a_id_sorted,
        crane_b_id=crane_b_id_sorted,
        crane_a_interval=interval_a if a_id == crane_a_id_sorted else interval_b,
        crane_b_interval=interval_b if b_id == crane_b_id_sorted else interval_a,
        distance_between_towers=dist,
        created_at=time.time(),
    )
    return sector


def rebuild_all_overlap_sectors():
    global overlap_sectors, sector_pair_index, token_statuses
    overlap_sectors.clear()
    sector_pair_index.clear()

    crane_ids = list(cranes_config.keys())
    for i in range(len(crane_ids)):
        for j in range(i + 1, len(crane_ids)):
            a_id, b_id = crane_ids[i], crane_ids[j]
            sector = detect_overlap_sectors_for_pair(a_id, b_id)
            if sector:
                overlap_sectors[sector.sector_id] = sector
                sector_pair_index[get_pair_key(a_id, b_id)] = sector.sector_id
                if sector.sector_id not in token_statuses:
                    token_statuses[sector.sector_id] = TokenStatus(sector_id=sector.sector_id)
                log_arb_event(EventType.SECTOR_DETECTED, sector_id=sector.sector_id, details={
                    "crane_a": sector.crane_a_id,
                    "crane_b": sector.crane_b_id,
                    "distance": sector.distance_between_towers,
                })


def get_crane_interval_for_sector(sector: OverlapSector, crane_id: str) -> Optional[AngleInterval]:
    if crane_id == sector.crane_a_id:
        return sector.crane_a_interval
    if crane_id == sector.crane_b_id:
        return sector.crane_b_interval
    return None


def find_sectors_for_crane_and_angle(crane_id: str, angle: float) -> List[Tuple[OverlapSector, AngleInterval]]:
    result = []
    for sector in overlap_sectors.values():
        if crane_id not in (sector.crane_a_id, sector.crane_b_id):
            continue
        interval = get_crane_interval_for_sector(sector, crane_id)
        if interval and angle_in_interval(angle, interval):
            result.append((sector, interval))
    return result


def find_sectors_for_crane(crane_id: str) -> List[OverlapSector]:
    result = []
    for sector in overlap_sectors.values():
        if crane_id in (sector.crane_a_id, sector.crane_b_id):
            result.append(sector)
    return result


def clean_expired_tokens_and_waiters():
    now = time.time()
    for sector_id, ts in list(token_statuses.items()):
        if ts.holder_crane_id and ts.expires_at and now > ts.expires_at:
            prev_holder = ts.holder_crane_id
            prev_acquired = ts.acquired_at
            log_arb_event(EventType.TOKEN_REVOKED, crane_id=prev_holder, sector_id=sector_id, details={
                "reason": "hold_timeout",
                "acquired_at": prev_acquired,
                "expires_at": ts.expires_at,
            })
            if prev_holder in cranes_held_tokens:
                cranes_held_tokens[prev_holder].discard(sector_id)
            ts.holder_crane_id = None
            ts.acquired_at = None
            ts.expires_at = None
            _try_grant_token_to_next(sector_id)

        valid_queue: List[WaitQueueItem] = []
        for item in ts.wait_queue:
            if now - item.requested_at > zone_arb_config.token_wait_timeout:
                log_arb_event(EventType.TOKEN_REQUEST_TIMEOUT, crane_id=item.crane_id, sector_id=sector_id, details={
                    "request_id": item.request_id,
                    "requested_at": item.requested_at,
                    "timeout_seconds": zone_arb_config.token_wait_timeout,
                })
                if item.crane_id in cranes_pending_requests:
                    cranes_pending_requests[item.crane_id].pop(sector_id, None)
            else:
                valid_queue.append(item)
        ts.wait_queue = valid_queue


def _try_grant_token_to_next(sector_id: str):
    ts = token_statuses.get(sector_id)
    if not ts or ts.holder_crane_id:
        return
    if not ts.wait_queue:
        return
    next_item = ts.wait_queue.pop(0)
    now = time.time()
    ts.holder_crane_id = next_item.crane_id
    ts.acquired_at = now
    ts.expires_at = now + zone_arb_config.token_max_hold_time
    if next_item.crane_id not in cranes_held_tokens:
        cranes_held_tokens[next_item.crane_id] = set()
    cranes_held_tokens[next_item.crane_id].add(sector_id)
    if next_item.crane_id in cranes_pending_requests:
        cranes_pending_requests[next_item.crane_id].pop(sector_id, None)
    log_arb_event(EventType.TOKEN_ACQUIRED, crane_id=next_item.crane_id, sector_id=sector_id, details={
        "request_id": next_item.request_id,
        "from_queue": True,
        "expires_at": ts.expires_at,
    })
    log_arb_event(EventType.TOKEN_DEQUEUED, crane_id=next_item.crane_id, sector_id=sector_id, details={
        "request_id": next_item.request_id,
    })


def request_token(crane_id: str, sector_id: str) -> Dict:
    clean_expired_tokens_and_waiters()
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    if sector_id not in overlap_sectors:
        raise HTTPException(status_code=404, detail=f"扇区 {sector_id} 不存在")
    sector = overlap_sectors[sector_id]
    if crane_id not in (sector.crane_a_id, sector.crane_b_id):
        raise HTTPException(status_code=400, detail=f"塔吊 {crane_id} 不属于扇区 {sector_id}")

    ts = token_statuses[sector_id]

    if ts.holder_crane_id == crane_id:
        return {
            "granted": True,
            "request_id": None,
            "sector_id": sector_id,
            "crane_id": crane_id,
            "message": "已持有该扇区令牌",
            "expires_at": ts.expires_at,
        }

    if crane_id in cranes_pending_requests and sector_id in cranes_pending_requests[crane_id]:
        req_id = cranes_pending_requests[crane_id][sector_id]
        for item in ts.wait_queue:
            if item.request_id == req_id:
                return {
                    "granted": False,
                    "request_id": req_id,
                    "sector_id": sector_id,
                    "crane_id": crane_id,
                    "queued": True,
                    "message": "令牌已在等待队列中",
                    "queue_position": ts.wait_queue.index(item) + 1,
                }

    request_id = str(uuid.uuid4())

    if ts.holder_crane_id is None:
        now = time.time()
        ts.holder_crane_id = crane_id
        ts.acquired_at = now
        ts.expires_at = now + zone_arb_config.token_max_hold_time
        if crane_id not in cranes_held_tokens:
            cranes_held_tokens[crane_id] = set()
        cranes_held_tokens[crane_id].add(sector_id)
        log_arb_event(EventType.TOKEN_ACQUIRED, crane_id=crane_id, sector_id=sector_id, details={
            "request_id": request_id,
            "from_queue": False,
            "expires_at": ts.expires_at,
        })
        return {
            "granted": True,
            "request_id": request_id,
            "sector_id": sector_id,
            "crane_id": crane_id,
            "message": "令牌授予成功",
            "expires_at": ts.expires_at,
        }
    else:
        item = WaitQueueItem(crane_id=crane_id, request_id=request_id, requested_at=time.time())
        ts.wait_queue.append(item)
        if crane_id not in cranes_pending_requests:
            cranes_pending_requests[crane_id] = {}
        cranes_pending_requests[crane_id][sector_id] = request_id
        log_arb_event(EventType.TOKEN_ENQUEUED, crane_id=crane_id, sector_id=sector_id, details={
            "request_id": request_id,
            "current_holder": ts.holder_crane_id,
            "queue_position": len(ts.wait_queue),
        })
        return {
            "granted": False,
            "request_id": request_id,
            "sector_id": sector_id,
            "crane_id": crane_id,
            "queued": True,
            "message": "令牌已被占用，已进入等待队列",
            "queue_position": len(ts.wait_queue),
            "wait_timeout_seconds": zone_arb_config.token_wait_timeout,
        }


def release_token(crane_id: str, sector_id: str, request_id: Optional[str] = None) -> Dict:
    clean_expired_tokens_and_waiters()
    if sector_id not in token_statuses:
        raise HTTPException(status_code=404, detail=f"扇区 {sector_id} 不存在")
    ts = token_statuses[sector_id]
    if ts.holder_crane_id != crane_id:
        still_pending = False
        if crane_id in cranes_pending_requests and sector_id in cranes_pending_requests[crane_id]:
            pending_req_id = cranes_pending_requests[crane_id][sector_id]
            ts.wait_queue = [i for i in ts.wait_queue if i.request_id != pending_req_id]
            cranes_pending_requests[crane_id].pop(sector_id, None)
            still_pending = True
        if not still_pending:
            return {
                "released": False,
                "sector_id": sector_id,
                "crane_id": crane_id,
                "message": f"塔吊 {crane_id} 未持有扇区 {sector_id} 的令牌",
            }
        else:
            log_arb_event(EventType.TOKEN_DEQUEUED, crane_id=crane_id, sector_id=sector_id, details={
                "request_id": request_id or pending_req_id,
                "reason": "voluntary_withdraw",
            })
            return {
                "released": True,
                "from_queue": True,
                "sector_id": sector_id,
                "crane_id": crane_id,
                "message": "已取消排队申请",
            }

    log_arb_event(EventType.TOKEN_RELEASED, crane_id=crane_id, sector_id=sector_id, details={
        "request_id": request_id,
        "held_for_seconds": time.time() - (ts.acquired_at or 0),
    })

    if crane_id in cranes_held_tokens:
        cranes_held_tokens[crane_id].discard(sector_id)

    ts.holder_crane_id = None
    ts.acquired_at = None
    ts.expires_at = None

    _try_grant_token_to_next(sector_id)

    return {
        "released": True,
        "sector_id": sector_id,
        "crane_id": crane_id,
        "message": "令牌释放成功",
    }


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

    clean_expired_tokens_and_waiters()

    rotation = normalize_angle(status.rotation_angle)
    sectors_in = find_sectors_for_crane_and_angle(status.crane_id, rotation)

    held_now = cranes_held_tokens.get(status.crane_id, set())
    prev_status = cranes_current_status.get(status.crane_id)
    prev_angle = normalize_angle(prev_status.rotation_angle) if prev_status else None
    prev_sectors_ids = set()
    if prev_angle is not None:
        prev_sectors = find_sectors_for_crane_and_angle(status.crane_id, prev_angle)
        prev_sectors_ids = {s.sector_id for s, _ in prev_sectors}

    for sector, interval in sectors_in:
        if sector.sector_id not in held_now:
            log_arb_event(EventType.STATUS_REJECTED_NO_TOKEN, crane_id=status.crane_id,
                          sector_id=sector.sector_id, details={
                              "rotation_angle": rotation,
                              "interval_start": interval.start,
                              "interval_end": interval.end,
                          })
            raise HTTPException(
                status_code=412,
                detail={
                    "message": "未持有该重叠扇区令牌，拒绝进入重叠区域，请先申请令牌",
                    "code": "NO_SECTOR_TOKEN",
                    "crane_id": status.crane_id,
                    "sector_id": sector.sector_id,
                    "rotation_angle": rotation,
                    "sector_angle_range": {
                        "start": interval.start,
                        "end": interval.end,
                        "wraps_zero": interval.wraps_zero,
                    },
                    "token_request_hint": f"请先调用 POST /api/arb/token/request 申请扇区 {sector.sector_id} 的令牌",
                }
            )

    for prev_sec_id in prev_sectors_ids:
        if prev_sec_id not in {s.sector_id for s, _ in sectors_in}:
            continue
        if prev_sec_id not in held_now:
            prev_sector_info = None
            for s, _ in sectors_in:
                if s.sector_id == prev_sec_id:
                    prev_sector_info = s
                    break
            if prev_sector_info:
                interval = get_crane_interval_for_sector(prev_sector_info, status.crane_id)
                if interval:
                    log_arb_event(EventType.STATUS_REJECTED_STILL_IN_ZONE, crane_id=status.crane_id,
                                  sector_id=prev_sec_id, details={
                                      "rotation_angle": rotation,
                                      "note": "token released but angle still in zone",
                                  })
                    raise HTTPException(
                        status_code=412,
                        detail={
                            "message": "令牌已释放但回转角度仍在重叠扇区内，请先转出该区域",
                            "code": "STILL_IN_OVERLAP_ZONE_AFTER_TOKEN_RELEASE",
                            "crane_id": status.crane_id,
                            "sector_id": prev_sec_id,
                            "rotation_angle": rotation,
                            "sector_angle_range": {
                                "start": interval.start,
                                "end": interval.end,
                                "wraps_zero": interval.wraps_zero,
                            },
                            "hint": "请将回转角度转出上述范围后再上报",
                        }
                    )

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
        "overlap_sectors_entered": [
            {
                "sector_id": s.sector_id,
                "other_crane": s.crane_b_id if s.crane_a_id == status.crane_id else s.crane_a_id,
                "token_held": True,
            }
            for s, _ in sectors_in
        ],
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
    clean_expired_tokens_and_waiters()
    return {
        "status": "ok",
        "service": "塔吊防碰撞联锁服务",
        "cranes_registered": len(cranes_config),
        "total_alarms": len(alarm_history),
        "locked_cranes": sum(1 for l in cranes_lock_status.values() if l.is_locked),
        "overlap_sectors": len(overlap_sectors),
        "active_tokens": sum(1 for t in token_statuses.values() if t.holder_crane_id),
        "timestamp": time.time(),
    }


@app.get("/api/arb/config", summary="查询作业区域仲裁配置")
def get_arb_config():
    return zone_arb_config


@app.put("/api/arb/config", summary="更新作业区域仲裁配置")
def update_arb_config(new_config: ZoneArbConfig):
    global zone_arb_config
    zone_arb_config = new_config
    return {
        "code": 0,
        "message": "配置更新成功",
        "config": zone_arb_config,
    }


@app.post("/api/arb/sectors/rebuild", summary="重新计算所有重叠扇区")
def api_rebuild_sectors():
    rebuild_all_overlap_sectors()
    return {
        "code": 0,
        "message": "重叠扇区重新计算完成",
        "sector_count": len(overlap_sectors),
        "sector_ids": list(overlap_sectors.keys()),
    }


@app.get("/api/arb/sectors", summary="查询所有重叠扇区定义及令牌状态")
def list_all_sectors(with_tokens: bool = True):
    clean_expired_tokens_and_waiters()
    result = []
    for sector in overlap_sectors.values():
        item = {
            "sector": sector,
        }
        if with_tokens:
            ts = token_statuses.get(sector.sector_id)
            item["token"] = ts
        result.append(item)
    return result


@app.get("/api/arb/sectors/{sector_id}", summary="查询单个重叠扇区详情")
def get_sector_detail(sector_id: str):
    clean_expired_tokens_and_waiters()
    if sector_id not in overlap_sectors:
        raise HTTPException(status_code=404, detail=f"扇区 {sector_id} 不存在")
    sector = overlap_sectors[sector_id]
    ts = token_statuses.get(sector_id)
    return {
        "sector": sector,
        "token_status": ts,
    }


@app.get("/api/arb/crane/{crane_id}/sectors", summary="查询某台塔吊涉及的所有重叠扇区")
def get_crane_sectors(crane_id: str, angle_hint: Optional[float] = None):
    clean_expired_tokens_and_waiters()
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    sectors = find_sectors_for_crane(crane_id)
    result = []
    for sector in sectors:
        interval = get_crane_interval_for_sector(sector, crane_id)
        ts = token_statuses.get(sector.sector_id)
        currently_in = False
        if angle_hint is not None and interval:
            currently_in = angle_in_interval(angle_hint, interval)
        result.append({
            "sector": sector,
            "crane_interval": interval,
            "currently_in_at_angle_hint": currently_in,
            "token_status": ts,
        })
    return result


@app.post("/api/arb/token/request", summary="申请重叠扇区令牌")
def api_request_token(req: TokenRequest):
    return request_token(req.crane_id, req.sector_id)


@app.post("/api/arb/token/release", summary="释放重叠扇区令牌")
def api_release_token(req: TokenReleaseRequest):
    return release_token(req.crane_id, req.sector_id, req.request_id)


@app.post("/api/arb/token/revoke/{sector_id}", summary="管理员强制收回某扇区令牌")
def api_revoke_token(sector_id: str, reason: Optional[str] = "admin_revoke"):
    clean_expired_tokens_and_waiters()
    if sector_id not in token_statuses:
        raise HTTPException(status_code=404, detail=f"扇区 {sector_id} 不存在")
    ts = token_statuses[sector_id]
    if not ts.holder_crane_id:
        return {
            "code": 0,
            "message": "该扇区当前没有持有令牌的塔吊",
            "sector_id": sector_id,
        }
    holder = ts.holder_crane_id
    log_arb_event(EventType.TOKEN_REVOKED, crane_id=holder, sector_id=sector_id, details={
        "reason": reason,
        "acquired_at": ts.acquired_at,
    })
    if holder in cranes_held_tokens:
        cranes_held_tokens[holder].discard(sector_id)
    ts.holder_crane_id = None
    ts.acquired_at = None
    ts.expires_at = None
    _try_grant_token_to_next(sector_id)
    return {
        "code": 0,
        "message": "令牌已强制收回",
        "sector_id": sector_id,
        "previous_holder": holder,
        "granted_to_next": token_statuses[sector_id].holder_crane_id,
    }


@app.get("/api/arb/crane/{crane_id}/tokens", summary="查询某台塔吊持有的所有令牌和待处理申请")
def get_crane_tokens(crane_id: str):
    clean_expired_tokens_and_waiters()
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")

    held: List[Dict] = []
    held_set = cranes_held_tokens.get(crane_id, set())
    for sec_id in held_set:
        ts = token_statuses.get(sec_id)
        if ts:
            held.append({
                "sector_id": sec_id,
                "acquired_at": ts.acquired_at,
                "expires_at": ts.expires_at,
                "remaining_seconds": (ts.expires_at - time.time()) if ts.expires_at else 0,
                "sector": overlap_sectors.get(sec_id),
            })

    pending: List[Dict] = []
    pending_map = cranes_pending_requests.get(crane_id, {})
    for sec_id, req_id in pending_map.items():
        ts = token_statuses.get(sec_id)
        pos = None
        req_at = None
        if ts:
            for idx, item in enumerate(ts.wait_queue):
                if item.request_id == req_id:
                    pos = idx + 1
                    req_at = item.requested_at
                    break
        pending.append({
            "sector_id": sec_id,
            "request_id": req_id,
            "queue_position": pos,
            "requested_at": req_at,
            "wait_elapsed_seconds": (time.time() - req_at) if req_at else 0,
            "timeout_seconds": zone_arb_config.token_wait_timeout,
            "sector": overlap_sectors.get(sec_id),
        })

    return CraneTokensView(
        crane_id=crane_id,
        held_tokens=held,
        pending_requests=pending,
    )


@app.get("/api/arb/tokens", summary="查询所有令牌状态(持有者+等待队列)")
def list_all_tokens():
    clean_expired_tokens_and_waiters()
    return list(token_statuses.values())


@app.get("/api/arb/events", summary="查询仲裁事件日志(可按塔吊或扇区过滤)")
def get_arb_events(crane_id: Optional[str] = None, sector_id: Optional[str] = None,
                   event_type: Optional[EventType] = None, limit: int = 200):
    result = arb_event_logs
    if crane_id:
        result = [e for e in result if e.crane_id == crane_id]
    if sector_id:
        result = [e for e in result if e.sector_id == sector_id]
    if event_type:
        result = [e for e in result if e.event_type == event_type]
    return result[-limit:]
