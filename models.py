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
    WIND_SPEED_WARNING = "WIND_SPEED_WARNING"
    WIND_SPEED_SHUTDOWN = "WIND_SPEED_SHUTDOWN"
    ENERGY_QUOTA_WARNING = "ENERGY_QUOTA_WARNING"
    ENERGY_QUOTA_EXCEEDED = "ENERGY_QUOTA_EXCEEDED"


class WindAlarmLevel(str, Enum):
    WARNING = "WARNING"
    SHUTDOWN = "SHUTDOWN"


class WindSpeedConfig(BaseModel):
    shutdown_threshold: float = Field(default=20.0, description="停机阈值(米/秒), 瞬时风速超过即锁定")
    warning_threshold: float = Field(default=15.0, description="预警阈值(米/秒), 60秒平均超过即预警")
    avg_window_seconds: int = Field(default=60, description="平均风速计算窗口(秒)")
    auto_recovery_consecutive_count: int = Field(default=10, description="自动恢复所需连续正常数据条数")
    auto_recovery_threshold_ratio: float = Field(default=0.8, description="自动恢复阈值为停机阈值的比例")
    max_records_per_crane: int = Field(default=120, description="每台塔吊保留的最大风速记录数")


class WindSpeedReport(BaseModel):
    crane_id: str = Field(description="塔吊ID")
    wind_speed: float = Field(description="瞬时风速(米/秒)")
    sensor_timestamp: float = Field(description="传感器上报时间戳(Unix秒)")


class WindSpeedRecord(BaseModel):
    crane_id: str
    wind_speed: float
    sensor_timestamp: float
    received_at: float
    datetime_str: str


class WindSpeedAlarmEvent(BaseModel):
    alarm_id: str
    alarm_type: AlarmType
    alarm_level: WindAlarmLevel
    crane_id: str
    timestamp: float
    datetime_str: str
    wind_speed: float
    avg_wind_speed_60s: Optional[float] = None
    threshold: float
    message: str
    details: Dict = {}


class WindSpeedRecoveryEvent(BaseModel):
    recovery_id: str
    crane_id: str
    recovery_time: float
    recovery_datetime_str: str
    max_wind_speed_during_shutdown: float
    avg_wind_speed_before_recovery: float
    shutdown_duration_seconds: float
    recovery_method: str
    message: str


class WindSpeedStatus(BaseModel):
    crane_id: str
    latest_wind_speed: Optional[float] = None
    latest_sensor_timestamp: Optional[float] = None
    latest_datetime_str: Optional[str] = None
    avg_wind_speed_60s: Optional[float] = None
    is_wind_shutdown: bool = False
    wind_shutdown_at: Optional[float] = None
    wind_shutdown_reason: Optional[str] = None
    consecutive_normal_count: int = 0
    current_config: WindSpeedConfig


class WindSpeedThresholdUpdateRequest(BaseModel):
    crane_id: Optional[str] = Field(default=None, description="塔吊ID，不指定则更新全局默认配置")
    shutdown_threshold: Optional[float] = Field(default=None, description="停机阈值(米/秒)")
    warning_threshold: Optional[float] = Field(default=None, description="预警阈值(米/秒)")
    avg_window_seconds: Optional[int] = Field(default=None, description="平均风速计算窗口(秒)")
    auto_recovery_consecutive_count: Optional[int] = Field(default=None, description="自动恢复所需连续正常数据条数")
    auto_recovery_threshold_ratio: Optional[float] = Field(default=None, description="自动恢复阈值为停机阈值的比例")
    max_records_per_crane: Optional[int] = Field(default=None, description="每台塔吊保留的最大风速记录数")


class MaintenanceStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    ABNORMAL = "ABNORMAL"


class MaintenanceType(str, Enum):
    ROUTINE = "ROUTINE"
    COMPREHENSIVE = "COMPREHENSIVE"
    EMERGENCY = "EMERGENCY"
    SEASONAL = "SEASONAL"


class MaintenanceAlarmType(str, Enum):
    MAINTENANCE_TIMEOUT = "MAINTENANCE_TIMEOUT"
    MAINTENANCE_DUE_SOON = "MAINTENANCE_DUE_SOON"
    MAINTENANCE_OVERDUE = "MAINTENANCE_OVERDUE"


class MaintenanceDailyStats(BaseModel):
    in_maintenance_period: bool = False
    maintenance_window_id: Optional[str] = None
    maintenance_type: Optional[MaintenanceType] = None
    suppressed_collision_alarms: int = 0
    supplied_anomaly_alarms: int = 0
    total_suppressed_alarms: int = 0


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


class DailyReportStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class DailyReportDataStatus(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


class AlarmStats(BaseModel):
    collision: int = 0
    rotation_oscillation: int = 0
    trolley_overspeed: int = 0
    load_moment_warning: int = 0
    wind_speed_warning: int = 0
    wind_speed_shutdown: int = 0
    energy_quota_warning: int = 0
    energy_quota_exceeded: int = 0


class FreezeLockStats(BaseModel):
    freeze_count: int = 0
    freeze_total_seconds: float = 0.0
    lock_count: int = 0
    lock_total_seconds: float = 0.0


class TokenStats(BaseModel):
    request_count: int = 0
    queue_count: int = 0
    avg_wait_seconds: float = 0.0


class InspectionDailyStats(BaseModel):
    inspection_completed: bool = False
    inspection_report_id: Optional[str] = None
    inspector: Optional[str] = None
    inspection_time: Optional[float] = None
    hazards_found: int = 0
    overdue_hazards: int = 0


class EnergyDailyStats(BaseModel):
    total_energy_kwh: float = 0.0
    peak_power_kw: float = 0.0
    avg_power_kw: float = 0.0
    yellow_alarm_count: int = 0
    red_alarm_count: int = 0
    over_limit: bool = False


class DailyReport(BaseModel):
    report_id: str
    crane_id: str
    report_date: str
    completed_orders: int = 0
    total_lifts: int = 0
    work_duration_seconds: float = 0.0
    first_report_time: Optional[float] = None
    last_report_time: Optional[float] = None
    alarm_stats: AlarmStats = AlarmStats()
    freeze_lock_stats: FreezeLockStats = FreezeLockStats()
    token_stats: TokenStats = TokenStats()
    maintenance_stats: MaintenanceDailyStats = MaintenanceDailyStats()
    inspection_stats: InspectionDailyStats = InspectionDailyStats()
    energy_stats: EnergyDailyStats = EnergyDailyStats()
    data_status: DailyReportDataStatus = DailyReportDataStatus.COMPLETE
    incomplete_orders: List[str] = []
    remarks: str = ""
    status: DailyReportStatus = DailyReportStatus.PENDING
    approver: Optional[str] = None
    approval_remarks: Optional[str] = None
    approved_at: Optional[float] = None
    generated_at: float
    updated_at: float


class DailyReportGenerateRequest(BaseModel):
    date: Optional[str] = Field(default=None, description="生成日期 YYYY-MM-DD，默认当天")
    crane_id: Optional[str] = Field(default=None, description="指定塔吊ID，不指定则生成所有塔吊")


class DailyReportApproveRequest(BaseModel):
    action: str = Field(description="审批动作: APPROVE 或 REJECT")
    approver: str = Field(description="审批人")
    remarks: Optional[str] = Field(default="", description="审批备注")


class DailyReportSummaryItem(BaseModel):
    crane_id: str
    crane_name: str
    total_reports: int
    total_completed_orders: int
    total_lifts: int
    total_work_seconds: float
    total_alarms: int
    total_freezes: int
    total_locks: int
    total_token_requests: int
    total_token_queues: int


class DailyReportSummaryResponse(BaseModel):
    start_date: str
    end_date: str
    summaries: List[DailyReportSummaryItem] = []


class MaintenanceWindowCreate(BaseModel):
    crane_id: str = Field(description="塔吊ID")
    start_time: float = Field(description="停机窗口开始时间(Unix时间戳)")
    end_time: float = Field(description="停机窗口结束时间(Unix时间戳)")
    maintenance_type: MaintenanceType = Field(default=MaintenanceType.ROUTINE, description="维保类型")
    responsible_person: str = Field(description="负责人")
    remarks: Optional[str] = Field(default="", description="备注")


class MaintenanceWindow(BaseModel):
    window_id: str
    crane_id: str
    start_time: float
    end_time: float
    maintenance_type: MaintenanceType
    responsible_person: str
    remarks: str = ""
    status: MaintenanceStatus = MaintenanceStatus.PENDING
    created_at: float
    updated_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    is_active: bool = False
    is_timeout: bool = False


class MaintenanceRecord(BaseModel):
    record_id: str
    window_id: str
    crane_id: str
    replaced_parts: List[str] = Field(default=[], description="更换部件列表")
    inspection_results: str = Field(description="检测结果描述")
    next_suggested_maintenance_date: Optional[float] = Field(default=None, description="下次建议维保时间(Unix时间戳)")
    confirmed_by: str = Field(description="确认人")
    confirmed_at: float
    remarks: str = ""


class MaintenanceConfirmRequest(BaseModel):
    replaced_parts: List[str] = Field(default=[], description="更换部件列表")
    inspection_results: str = Field(description="检测结果描述")
    next_suggested_maintenance_date: Optional[float] = Field(default=None, description="下次建议维保时间(Unix时间戳)")
    confirmed_by: str = Field(description="确认人")
    remarks: Optional[str] = Field(default="", description="备注")


class MaintenanceAlarmEvent(BaseModel):
    alarm_id: str
    alarm_type: MaintenanceAlarmType
    window_id: str
    crane_id: str
    timestamp: float
    datetime_str: str
    message: str
    details: Dict = {}


class CraneMaintenanceStatus(BaseModel):
    crane_id: str
    crane_name: str
    last_maintenance_time: Optional[float] = None
    next_due_date: Optional[float] = None
    days_until_due: Optional[int] = None
    current_window: Optional[MaintenanceWindow] = None
    maintenance_status: MaintenanceStatus = MaintenanceStatus.COMPLETED
    maintenance_history_count: int = 0
    cycle_days: int = 30


class MaintenanceWindowQuery(BaseModel):
    crane_id: Optional[str] = Field(default=None, description="按塔吊ID筛选")
    status: Optional[MaintenanceStatus] = Field(default=None, description="按状态筛选")
    maintenance_type: Optional[MaintenanceType] = Field(default=None, description="按维保类型筛选")
    start_from: Optional[float] = Field(default=None, description="窗口开始时间下限")
    end_before: Optional[float] = Field(default=None, description="窗口结束时间上限")


class OperatorGrade(str, Enum):
    PRIMARY = "PRIMARY"
    INTERMEDIATE = "INTERMEDIATE"
    ADVANCED = "ADVANCED"


GRADE_ARM_LENGTH_LIMITS: Dict[OperatorGrade, Optional[float]] = {
    OperatorGrade.PRIMARY: 50.0,
    OperatorGrade.INTERMEDIATE: 60.0,
    OperatorGrade.ADVANCED: None,
}

ASSESSMENT_PASSING_SCORE = 60
ASSESSMENT_FULL_SCORE = 100
ASSESSMENT_VALIDITY_MONTHS = 6


class OperatorCreate(BaseModel):
    name: str = Field(description="操作员姓名")
    grade: OperatorGrade = Field(description="资质等级: PRIMARY/INTERMEDIATE/ADVANCED")
    phone: Optional[str] = Field(default=None, description="联系电话")
    id_number: Optional[str] = Field(default=None, description="身份证号")


class OperatorUpdate(BaseModel):
    name: Optional[str] = Field(default=None, description="操作员姓名")
    grade: Optional[OperatorGrade] = Field(default=None, description="资质等级")
    phone: Optional[str] = Field(default=None, description="联系电话")
    id_number: Optional[str] = Field(default=None, description="身份证号")


class Operator(BaseModel):
    operator_id: str
    name: str
    grade: OperatorGrade
    phone: Optional[str] = None
    id_number: Optional[str] = None
    created_at: float
    updated_at: float


class AssessmentRecord(BaseModel):
    assessment_id: str
    operator_id: str
    score: int = Field(ge=0, le=100, description="考核分数(0-100)")
    passed: bool
    assessed_at: float
    valid_until: Optional[float] = None
    assessor: Optional[str] = None
    remarks: str = ""


class AssessmentCreate(BaseModel):
    score: int = Field(ge=0, le=100, description="考核分数(0-100)")
    assessor: Optional[str] = Field(default=None, description="考核人")
    remarks: Optional[str] = Field(default="", description="备注")


class CraneOperatorBinding(BaseModel):
    crane_id: str
    operator_id: str
    bound_at: float
    unbound_at: Optional[float] = None
    is_active: bool = True


class OperatorBindRequest(BaseModel):
    operator_id: str = Field(description="操作员ID")


class ShiftHandoverRequest(BaseModel):
    from_operator_id: str = Field(description="交班操作员ID")
    to_operator_id: str = Field(description="接班操作员ID")
    remarks: Optional[str] = Field(default="", description="交接备注(有未完成工单或未解除告警时必填)")


class ShiftHandoverRecord(BaseModel):
    handover_id: str
    crane_id: str
    from_operator_id: str
    to_operator_id: str
    from_operator_name: str
    to_operator_name: str
    has_pending_orders: bool
    has_unresolved_alarms: bool
    pending_order_ids: List[str] = []
    unresolved_alarm_ids: List[str] = []
    remarks: str
    handed_over_at: float


class OperatorAttendanceSegment(BaseModel):
    operator_id: str
    operator_name: str
    start_time: float
    end_time: Optional[float] = None
    is_current: bool = False


class OperatorQualificationStatus(BaseModel):
    operator_id: str
    operator_name: str
    grade: OperatorGrade
    is_qualified: bool
    latest_assessment: Optional[AssessmentRecord] = None
    qualification_expiry: Optional[float] = None
    bound_crane_id: Optional[str] = None
    arm_length_limit: Optional[float] = Field(default=None, description="可操作臂长上限(米), None表示不限")


class PathDirection(str, Enum):
    CW = "CW"
    CCW = "CCW"


class PathSegmentStatus(str, Enum):
    CLEAR = "CLEAR"
    CONFLICT = "CONFLICT"
    UNREACHABLE = "UNREACHABLE"


class PathSegment(BaseModel):
    segment_index: int
    start_angle: float = Field(description="段起始角度(度)")
    end_angle: float = Field(description="段结束角度(度)")
    sector_ids: List[str] = Field(default=[], description="途经重叠扇区ID列表")
    required_tokens: List[str] = Field(default=[], description="需要申请的令牌(扇区ID)列表")
    estimated_time_seconds: float = Field(description="预估通过时间(秒)")


class PathPlan(BaseModel):
    plan_id: str
    order_id: str
    crane_id: str
    lift_angle: float = Field(description="起吊点方位角(度)")
    drop_angle: float = Field(description="落点方位角(度)")
    direction: PathDirection
    segments: List[PathSegment] = []
    total_estimated_time: float = Field(description="总预估通过时间(秒)")
    angular_distance: float = Field(description="回转角度距离(度)")
    created_at: float


class PathSegmentRehearsal(BaseModel):
    segment_index: int
    start_angle: float
    end_angle: float
    sector_ids: List[str] = []
    required_tokens: List[str] = []
    estimated_time_seconds: float
    status: PathSegmentStatus
    conflict_token_holder: Optional[str] = None
    estimated_wait_seconds: float = 0.0


class PathRehearsalResult(BaseModel):
    order_id: str
    crane_id: str
    main_path: List[PathSegmentRehearsal] = []
    main_path_total_time: float = Field(description="主路径预估总耗时(秒,含等待)")
    has_conflict: bool
    alternative_path: Optional[List[PathSegmentRehearsal]] = None
    alternative_direction: Optional[PathDirection] = None
    alternative_path_total_time: Optional[float] = None


class PathExecutionRecord(BaseModel):
    record_id: str
    plan_id: str
    order_id: str
    crane_id: str
    direction: PathDirection
    estimated_total_time: float
    actual_total_time: float
    deviation_seconds: float
    segment_count: int
    executed_at: float
    completed_at: float


class PathPlanConfirmRequest(BaseModel):
    direction: PathDirection = Field(description="选择的路径方向: CW(主路径) 或 CCW(备选路径)")


class InspectionItemResult(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    OBSERVE = "OBSERVE"


class HazardSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class HazardStatus(str, Enum):
    PENDING_RECTIFICATION = "PENDING_RECTIFICATION"
    RECTIFYING = "RECTIFYING"
    PENDING_REVIEW = "PENDING_REVIEW"
    CLOSED = "CLOSED"


class StandardInspectionItem(BaseModel):
    item_id: str
    item_name: str
    category: str
    description: str
    default_severity: HazardSeverity = HazardSeverity.MEDIUM


class InspectionItemEntry(BaseModel):
    item_id: str
    result: InspectionItemResult
    remark: Optional[str] = Field(default="", description="备注说明")


class InspectionReportCreate(BaseModel):
    crane_id: str = Field(description="塔吊ID")
    inspector: str = Field(description="巡检人")
    items: List[InspectionItemEntry] = Field(description="逐项检查结果")
    remark: Optional[str] = Field(default="", description="巡检总体备注")


class InspectionReport(BaseModel):
    report_id: str
    crane_id: str
    crane_name: str
    inspector: str
    inspection_time: float
    inspection_date: str
    items: List[InspectionItemEntry]
    total_items: int
    pass_count: int
    fail_count: int
    observe_count: int
    remark: str
    created_at: float


class HazardCreate(BaseModel):
    crane_id: str = Field(description="塔吊ID")
    source_report_id: str = Field(description="来源巡检报告ID")
    item_id: str = Field(description="关联检查项ID")
    item_name: str = Field(description="检查项名称")
    description: str = Field(description="隐患描述")
    severity: HazardSeverity = Field(default=HazardSeverity.MEDIUM, description="严重程度")
    responsible_person: str = Field(description="整改责任人")
    deadline: Optional[float] = Field(default=None, description="整改期限(Unix时间戳, 默认48小时)")


class Hazard(BaseModel):
    hazard_id: str
    crane_id: str
    crane_name: str
    source_report_id: str
    item_id: str
    item_name: str
    description: str
    severity: HazardSeverity
    status: HazardStatus
    responsible_person: str
    deadline: float
    created_at: float
    updated_at: float
    accepted_at: Optional[float] = None
    rectification_remark: Optional[str] = None
    rectification_submitted_at: Optional[float] = None
    review_result: Optional[bool] = None
    review_remark: Optional[str] = None
    reviewed_at: Optional[float] = None
    reject_reason: Optional[str] = None
    reject_count: int = 0
    is_overdue: bool = False
    overdue_alarm_generated: bool = False


class HazardAcceptRequest(BaseModel):
    responsible_person: str = Field(description="责任人确认")


class HazardSubmitRequest(BaseModel):
    rectification_remark: str = Field(description="整改说明")


class HazardReviewRequest(BaseModel):
    reviewer: str = Field(description="复查人")
    passed: bool = Field(description="复查是否通过")
    review_remark: Optional[str] = Field(default="", description="复查备注")
    reject_reason: Optional[str] = Field(default="", description="打回原因(复查不通过时必填)")


class CraneHazardStats(BaseModel):
    crane_id: str
    crane_name: str
    total_hazards: int = 0
    pending_count: int = 0
    rectifying_count: int = 0
    review_count: int = 0
    closed_count: int = 0
    overdue_count: int = 0
    avg_close_hours: float = 0.0


class CooperativeLiftStatus(str, Enum):
    PENDING_READY = "PENDING_READY"
    SYNCHRONIZING = "SYNCHRONIZING"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class CraneLiftAssignment(BaseModel):
    crane_id: str
    hook_offset_x: float = Field(description="挂点相对构件重心的X方向偏移距离(米)")
    hook_offset_y: float = Field(description="挂点相对构件重心的Y方向偏移距离(米)")
    load_ratio: float = Field(description="承担的载荷比例(0-1之间, 如0.6表示60%)")


class ComponentParams(BaseModel):
    weight: float = Field(description="构件总重量(吨)")
    length: float = Field(description="构件长度(米)")
    center_of_gravity_offset_x: float = Field(default=0.0, description="重心X方向偏移量(米)")
    center_of_gravity_offset_y: float = Field(default=0.0, description="重心Y方向偏移量(米)")


class CooperativeLiftCreate(BaseModel):
    component: ComponentParams
    crane_assignments: List[CraneLiftAssignment] = Field(description="参与塔吊及挂点配置, 至少2台")
    initiator: str = Field(description="任务发起人")
    lift_x: float = Field(description="起吊点X坐标(米)")
    lift_y: float = Field(description="起吊点Y坐标(米)")
    drop_x: float = Field(description="落点X坐标(米)")
    drop_y: float = Field(description="落点Y坐标(米)")
    estimated_duration: float = Field(description="预计耗时(分钟)")
    height_diff_threshold: float = Field(default=0.5, description="允许的吊钩高度差阈值(米), 默认0.5米")
    height_diff_duration_threshold: float = Field(default=3.0, description="高度差超限持续时间阈值(秒), 默认3秒")


class CooperativeLiftReadyConfirm(BaseModel):
    crane_id: str
    operator_id: str


class CooperativeLiftSyncAck(BaseModel):
    crane_id: str
    operator_id: str


class CooperativeLiftCompleteConfirm(BaseModel):
    initiator: str


class HeightDesyncAlarm(BaseModel):
    alarm_id: str
    task_id: str
    timestamp: float
    datetime_str: str
    crane_a_id: str
    crane_b_id: str
    height_diff: float
    threshold: float
    duration: float


class CooperativeLiftTask(BaseModel):
    task_id: str
    component: ComponentParams
    crane_assignments: List[CraneLiftAssignment]
    initiator: str
    lift_x: float
    lift_y: float
    drop_x: float
    drop_y: float
    estimated_duration: float
    height_diff_threshold: float
    height_diff_duration_threshold: float
    status: CooperativeLiftStatus = CooperativeLiftStatus.PENDING_READY
    created_at: float
    updated_at: float
    ready_cranes: List[str] = []
    sync_command_sent_at: Optional[float] = None
    sync_acked_cranes: List[str] = []
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    aborted_at: Optional[float] = None
    abort_reason: Optional[str] = None
    height_desync_alarms: List[HeightDesyncAlarm] = []
    locked_cranes: List[str] = []


class OverloadAlarmLevel(str, Enum):
    YELLOW = "YELLOW"
    ORANGE = "ORANGE"
    RED = "RED"


class LoadMomentEnvelopePoint(BaseModel):
    distance: float = Field(description="变幅距离(米)")
    max_load: float = Field(description="该距离对应的最大允许载荷(吨)")


class WeightSensorReport(BaseModel):
    crane_id: str = Field(description="塔吊ID")
    weight: float = Field(description="实时重量(吨)")
    sensor_timestamp: float = Field(description="传感器上报时间戳(Unix秒)")


class WeightRecord(BaseModel):
    crane_id: str
    weight: float
    sensor_timestamp: float
    received_at: float
    datetime_str: str
    trolley_position: Optional[float] = Field(default=None, description="对应的变幅小车位置(米)")
    allowed_load: Optional[float] = Field(default=None, description="当前位置允许的最大载荷(吨)")
    overload_ratio: Optional[float] = Field(default=None, description="超载比例(实际/允许)")


class OverloadAlarmEvent(BaseModel):
    alarm_id: str
    crane_id: str
    timestamp: float
    datetime_str: str
    alarm_level: OverloadAlarmLevel
    realtime_weight: float
    allowed_load: float
    trolley_position: float
    overload_ratio: float
    message: str
    crane_locked: bool = False
    emergency_notification: bool = False


class RealtimeLoadStatus(BaseModel):
    crane_id: str
    latest_weight: Optional[float] = None
    latest_sensor_timestamp: Optional[float] = None
    latest_datetime_str: Optional[str] = None
    current_trolley_position: Optional[float] = None
    allowed_load: Optional[float] = None
    overload_ratio: Optional[float] = None
    alarm_level: Optional[OverloadAlarmLevel] = None
    is_locked: bool = False


class EnvelopeUpdateRequest(BaseModel):
    crane_id: str = Field(description="塔吊ID")
    envelope_points: List[LoadMomentEnvelopePoint] = Field(description="力矩包络曲线采样点列表，至少5个点，按变幅距离升序排列")


class EnergyAlarmLevel(str, Enum):
    YELLOW = "YELLOW"
    RED = "RED"


class EnergyMeterReport(BaseModel):
    crane_id: str = Field(description="塔吊ID")
    instantaneous_power_kw: float = Field(description="瞬时功率(千瓦)")
    cumulative_energy_kwh: float = Field(description="累计电量(千瓦时)")
    sensor_timestamp: float = Field(description="电表上报时间戳(Unix秒)")


class EnergyMeterRecord(BaseModel):
    crane_id: str
    instantaneous_power_kw: float
    cumulative_energy_kwh: float
    sensor_timestamp: float
    received_at: float
    datetime_str: str


class EnergyAlarmEvent(BaseModel):
    alarm_id: str
    alarm_type: AlarmType
    alarm_level: EnergyAlarmLevel
    crane_id: str
    timestamp: float
    datetime_str: str
    cumulative_energy_kwh: float
    quota_kwh: float
    quota_usage_ratio: float
    message: str
    details: Dict = {}


class EnergyCraneStatus(BaseModel):
    crane_id: str
    instantaneous_power_kw: Optional[float] = None
    daily_cumulative_kwh: float = 0.0
    quota_kwh: float = 500.0
    quota_remaining_kwh: float = 500.0
    quota_usage_ratio: float = 0.0
    efficiency_ratio: Optional[float] = None
    is_over_limit: bool = False
    latest_sensor_timestamp: Optional[float] = None
    latest_datetime_str: Optional[str] = None
    current_work_order_id: Optional[str] = None
    current_work_weight_tons: Optional[float] = None


class EnergyQuotaUpdateRequest(BaseModel):
    crane_id: Optional[str] = Field(default=None, description="塔吊ID，不指定则更新全局默认配额")
    daily_quota_kwh: float = Field(description="每日能耗配额(千瓦时)")
