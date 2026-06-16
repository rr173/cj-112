from typing import Dict, List, Optional
from enum import Enum

from pydantic import BaseModel, Field


class ZoneArbConfig(BaseModel):
    token_wait_timeout: float = Field(default=30.0, description="令牌申请等待超时时间(秒)")
    token_max_hold_time: float = Field(default=120.0, description="令牌最大占用时长(秒)")


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


class AlarmType(str, Enum):
    COLLISION = "COLLISION"
    ROTATION_OSCILLATION = "ROTATION_OSCILLATION"
    TROLLEY_OVERSPEED = "TROLLEY_OVERSPEED"
    LOAD_MOMENT_WARNING = "LOAD_MOMENT_WARNING"


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
    alarm_type: AlarmType = AlarmType.COLLISION
    timestamp: float
    datetime_str: str
    crane_a_id: str
    crane_b_id: str
    distance: Optional[float] = None
    safety_threshold: Optional[float] = None
    crane_a_snapshot: Optional[CoordinateSnapshot] = None
    crane_b_snapshot: Optional[CoordinateSnapshot] = None
    message: str
    details: Dict = {}


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


class WorkOrderPriority(str, Enum):
    URGENT = "URGENT"
    NORMAL = "NORMAL"
    LOW = "LOW"


class WorkOrderStatus(str, Enum):
    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class WorkOrderCreate(BaseModel):
    lift_x: float = Field(description="起吊点X坐标(米)")
    lift_y: float = Field(description="起吊点Y坐标(米)")
    drop_x: float = Field(description="落点X坐标(米)")
    drop_y: float = Field(description="落点Y坐标(米)")
    weight: float = Field(description="预估重量(吨)")
    priority: WorkOrderPriority = Field(default=WorkOrderPriority.NORMAL, description="优先级")
    estimated_duration: float = Field(description="预计耗时(分钟)")


class WorkOrder(BaseModel):
    order_id: str
    lift_x: float
    lift_y: float
    drop_x: float
    drop_y: float
    weight: float
    priority: WorkOrderPriority
    estimated_duration: float
    status: WorkOrderStatus = WorkOrderStatus.PENDING
    assigned_crane_id: Optional[str] = None
    created_at: float
    updated_at: float
    assigned_at: Optional[float] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    cancelled_at: Optional[float] = None
    acquired_sectors: List[str] = []
    failure_reason: Optional[str] = None


class WorkOrderManualAssign(BaseModel):
    crane_id: str = Field(description="指定的塔吊ID")


class AnomalyDetectionConfig(BaseModel):
    sliding_window_size: int = Field(default=600, description="滑动窗口大小(最近N条上报记录)")
    rotation_reversal_threshold: int = Field(default=10, description="回转震荡反转次数阈值(次/分钟)")
    max_trolley_speed: float = Field(default=2.0, description="最大变幅速度(米/秒)")
    trolley_overspeed_count: int = Field(default=3, description="连续超速次数阈值")
    load_moment_ratio_threshold: float = Field(default=0.7, description="力矩预警阈值(最大力矩的比例)")
    load_moment_duration_threshold: float = Field(default=5.0, description="力矩超限持续时间阈值(秒)")
    rotation_freeze_duration: float = Field(default=3.0, description="回转震荡告警后冻结时长(秒)")


class CraneStatusRecord(BaseModel):
    crane_id: str
    rotation_angle: float
    trolley_position: float
    hook_height: float
    timestamp: float


class SlidingWindowStats(BaseModel):
    crane_id: str
    window_size: int
    current_count: int
    avg_rotation_speed: float
    avg_trolley_speed: float
    current_moment: float
    max_moment: float
    moment_ratio: float
    alarm_count_in_window: int
    first_timestamp: Optional[float] = None
    last_timestamp: Optional[float] = None
    rotation_reversal_count: int = 0
    trolley_overspeed_count: int = 0


class AnomalyEvent(BaseModel):
    event_id: str
    alarm_type: AlarmType
    timestamp: float
    datetime_str: str
    crane_id: str
    message: str
    details: Dict
    resolved: bool = False
    resolved_at: Optional[float] = None


class CraneFreezeStatus(BaseModel):
    crane_id: str
    is_frozen: bool
    frozen_at: Optional[float] = None
    frozen_reason: Optional[str] = None
    unfreeze_at: Optional[float] = None
