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
