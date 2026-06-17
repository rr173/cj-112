import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from models import (
    StandardInspectionItem,
    InspectionItemResult,
    HazardSeverity,
    HazardStatus,
    InspectionReport,
    InspectionReportCreate,
    Hazard,
    HazardCreate,
    CraneHazardStats,
    InspectionDailyStats,
)
from collision import cranes_config


standard_inspection_items: List[StandardInspectionItem] = []

inspection_reports: Dict[str, InspectionReport] = {}

hazards: Dict[str, Hazard] = {}


def init_inspection_module():
    global standard_inspection_items
    standard_inspection_items = [
        StandardInspectionItem(
            item_id="WIRE-ROPE-001",
            item_name="钢丝绳磨损检查",
            category="结构件",
            description="检查钢丝绳断丝数、磨损程度、腐蚀情况，是否超过报废标准",
            default_severity=HazardSeverity.CRITICAL,
        ),
        StandardInspectionItem(
            item_id="WIRE-ROPE-002",
            item_name="钢丝绳润滑状态",
            category="结构件",
            description="检查钢丝绳润滑是否良好，有无干涩、锈蚀现象",
            default_severity=HazardSeverity.LOW,
        ),
        StandardInspectionItem(
            item_id="HOOK-001",
            item_name="吊钩防脱装置",
            category="吊具",
            description="检查吊钩防脱棘爪是否完好、动作灵活，弹簧是否有效",
            default_severity=HazardSeverity.CRITICAL,
        ),
        StandardInspectionItem(
            item_id="HOOK-002",
            item_name="吊钩磨损与变形",
            category="吊具",
            description="检查吊钩钩口磨损量、开口度变形、有无裂纹",
            default_severity=HazardSeverity.HIGH,
        ),
        StandardInspectionItem(
            item_id="LIMITER-001",
            item_name="起升高度限位器",
            category="安全装置",
            description="检查起升高度限位器动作是否灵敏可靠，能否切断动力",
            default_severity=HazardSeverity.CRITICAL,
        ),
        StandardInspectionItem(
            item_id="LIMITER-002",
            item_name="变幅限位器",
            category="安全装置",
            description="检查变幅小车内外限位器动作是否可靠",
            default_severity=HazardSeverity.HIGH,
        ),
        StandardInspectionItem(
            item_id="LIMITER-003",
            item_name="回转限位器",
            category="安全装置",
            description="检查回转限位器是否有效，左右回转角度是否在限制范围内",
            default_severity=HazardSeverity.MEDIUM,
        ),
        StandardInspectionItem(
            item_id="LIMITER-004",
            item_name="力矩限制器",
            category="安全装置",
            description="检查力矩限制器显示与实际是否一致，超载保护功能是否正常",
            default_severity=HazardSeverity.CRITICAL,
        ),
        StandardInspectionItem(
            item_id="LIMITER-005",
            item_name="起重量限制器",
            category="安全装置",
            description="检查起重量限制器动作是否灵敏可靠，能否报警和切断上升",
            default_severity=HazardSeverity.HIGH,
        ),
        StandardInspectionItem(
            item_id="FOUNDATION-001",
            item_name="基础螺栓紧固",
            category="基础结构",
            description="检查塔身基础连接螺栓有无松动、缺失，扭矩是否达标",
            default_severity=HazardSeverity.CRITICAL,
        ),
        StandardInspectionItem(
            item_id="FOUNDATION-002",
            item_name="基础沉降观测",
            category="基础结构",
            description="检查基础有无沉降、倾斜，塔身垂直度是否在允许范围内",
            default_severity=HazardSeverity.HIGH,
        ),
        StandardInspectionItem(
            item_id="ELECTRIC-001",
            item_name="电气线路绝缘",
            category="电气系统",
            description="检查电气线路绝缘层是否完好，有无破损、裸露情况",
            default_severity=HazardSeverity.HIGH,
        ),
        StandardInspectionItem(
            item_id="ELECTRIC-002",
            item_name="接地与防雷装置",
            category="电气系统",
            description="检查接地装置连接是否可靠，避雷针及引下线是否完好",
            default_severity=HazardSeverity.HIGH,
        ),
        StandardInspectionItem(
            item_id="ELECTRIC-003",
            item_name="配电箱防护",
            category="电气系统",
            description="检查配电箱门锁是否完好，有无防水、防尘措施",
            default_severity=HazardSeverity.MEDIUM,
        ),
        StandardInspectionItem(
            item_id="ELECTRIC-004",
            item_name="紧急断电开关",
            category="电气系统",
            description="检查紧急断电开关是否有效，能否迅速切断总电源",
            default_severity=HazardSeverity.CRITICAL,
        ),
        StandardInspectionItem(
            item_id="STRUCTURE-001",
            item_name="标准节连接螺栓",
            category="结构件",
            description="检查标准节连接螺栓有无松动、缺失，有无锈蚀",
            default_severity=HazardSeverity.HIGH,
        ),
        StandardInspectionItem(
            item_id="STRUCTURE-002",
            item_name="臂架连接销轴",
            category="结构件",
            description="检查臂架连接销轴定位是否可靠，开口销是否齐全",
            default_severity=HazardSeverity.HIGH,
        ),
        StandardInspectionItem(
            item_id="MECHANISM-001",
            item_name="起升机构制动器",
            category="机械传动",
            description="检查起升机构制动器制动片磨损量、制动力矩是否达标",
            default_severity=HazardSeverity.CRITICAL,
        ),
        StandardInspectionItem(
            item_id="MECHANISM-002",
            item_name="回转机构减速器",
            category="机械传动",
            description="检查回转机构减速器油量、有无异常噪音和渗漏",
            default_severity=HazardSeverity.MEDIUM,
        ),
        StandardInspectionItem(
            item_id="SAFETY-001",
            item_name="风速仪",
            category="安全装置",
            description="检查风速仪显示是否正常，报警功能是否可靠",
            default_severity=HazardSeverity.MEDIUM,
        ),
        StandardInspectionItem(
            item_id="SAFETY-002",
            item_name="障碍指示灯",
            category="安全装置",
            description="检查塔顶障碍指示灯是否完好、夜间是否正常亮起",
            default_severity=HazardSeverity.LOW,
        ),
    ]
    print(f"[巡检模块] 已加载 {len(standard_inspection_items)} 项标准检查项")


def get_standard_inspection_items() -> List[StandardInspectionItem]:
    return standard_inspection_items


def get_standard_item_by_id(item_id: str) -> Optional[StandardInspectionItem]:
    for item in standard_inspection_items:
        if item.item_id == item_id:
            return item
    return None


def _get_date_str(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


def _get_crane_name(crane_id: str) -> str:
    config = cranes_config.get(crane_id)
    return config.name if config else crane_id


def create_inspection_report(create: InspectionReportCreate) -> Dict:
    if create.crane_id not in cranes_config:
        return {"error": f"塔吊 {create.crane_id} 不存在"}

    item_ids_in_create = {item.item_id for item in create.items}
    std_item_ids = {item.item_id for item in standard_inspection_items}
    missing_ids = std_item_ids - item_ids_in_create
    if missing_ids:
        return {"error": f"缺少以下检查项: {', '.join(sorted(missing_ids))}"}

    invalid_ids = item_ids_in_create - std_item_ids
    if invalid_ids:
        return {"error": f"包含无效检查项ID: {', '.join(sorted(invalid_ids))}"}

    now = time.time()
    report_id = str(uuid.uuid4())
    date_str = _get_date_str(now)

    pass_count = sum(1 for item in create.items if item.result == InspectionItemResult.PASS)
    fail_count = sum(1 for item in create.items if item.result == InspectionItemResult.FAIL)
    observe_count = sum(1 for item in create.items if item.result == InspectionItemResult.OBSERVE)

    report = InspectionReport(
        report_id=report_id,
        crane_id=create.crane_id,
        crane_name=_get_crane_name(create.crane_id),
        inspector=create.inspector,
        inspection_time=now,
        inspection_date=date_str,
        items=create.items,
        total_items=len(create.items),
        pass_count=pass_count,
        fail_count=fail_count,
        observe_count=observe_count,
        remark=create.remark or "",
        created_at=now,
    )
    inspection_reports[report_id] = report

    created_hazards = []
    for item in create.items:
        if item.result == InspectionItemResult.FAIL:
            std_item = get_standard_item_by_id(item.item_id)
            if std_item:
                hazard = _create_hazard_from_inspection(
                    report, std_item, item, now
                )
                hazards[hazard.hazard_id] = hazard
                created_hazards.append(hazard)

    return {
        "message": "巡检报告提交成功" if fail_count == 0 else f"巡检报告提交成功，发现 {fail_count} 项不合格，已生成 {len(created_hazards)} 条隐患记录",
        "report": report,
        "created_hazards_count": len(created_hazards),
        "created_hazards": created_hazards,
    }


def _create_hazard_from_inspection(
    report: InspectionReport,
    std_item: StandardInspectionItem,
    entry,
    now: float,
) -> Hazard:
    deadline = now + 48 * 3600
    description = std_item.description
    if entry.remark:
        description = f"{description}。现场备注: {entry.remark}"

    hazard = Hazard(
        hazard_id=str(uuid.uuid4()),
        crane_id=report.crane_id,
        crane_name=report.crane_name,
        source_report_id=report.report_id,
        item_id=std_item.item_id,
        item_name=std_item.item_name,
        description=description,
        severity=std_item.default_severity,
        status=HazardStatus.PENDING_RECTIFICATION,
        responsible_person="待指定",
        deadline=deadline,
        created_at=now,
        updated_at=now,
    )
    return hazard


def get_inspection_report(report_id: str) -> Optional[InspectionReport]:
    return inspection_reports.get(report_id)


def get_inspection_history(
    crane_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[InspectionReport]:
    reports = list(inspection_reports.values())
    if crane_id:
        reports = [r for r in reports if r.crane_id == crane_id]
    if start_date:
        reports = [r for r in reports if r.inspection_date >= start_date]
    if end_date:
        reports = [r for r in reports if r.inspection_date <= end_date]
    reports.sort(key=lambda r: r.inspection_time, reverse=True)
    return reports


def assign_hazard_responsibility(
    hazard_id: str, responsible_person: str, deadline: Optional[float] = None
) -> Dict:
    hazard = hazards.get(hazard_id)
    if not hazard:
        return {"error": f"隐患 {hazard_id} 不存在"}
    if hazard.status == HazardStatus.CLOSED:
        return {"error": "隐患已关闭，不可操作"}

    now = time.time()
    hazard.responsible_person = responsible_person
    if deadline is not None:
        hazard.deadline = deadline
    hazard.updated_at = now
    return {"success": True, "hazard": hazard}


def accept_hazard(hazard_id: str, responsible_person: str) -> Dict:
    hazard = hazards.get(hazard_id)
    if not hazard:
        return {"error": f"隐患 {hazard_id} 不存在"}
    if hazard.status != HazardStatus.PENDING_RECTIFICATION:
        return {"error": f"隐患当前状态为 {hazard.status.value}，不可接单"}
    if hazard.responsible_person != responsible_person:
        return {"error": f"该隐患的责任人是 {hazard.responsible_person}，您无权接单"}

    now = time.time()
    hazard.status = HazardStatus.RECTIFYING
    hazard.accepted_at = now
    hazard.updated_at = now
    return {"success": True, "message": "已接单，开始整改", "hazard": hazard}


def submit_hazard_rectification(
    hazard_id: str, rectification_remark: str
) -> Dict:
    hazard = hazards.get(hazard_id)
    if not hazard:
        return {"error": f"隐患 {hazard_id} 不存在"}
    if hazard.status != HazardStatus.RECTIFYING:
        return {"error": f"隐患当前状态为 {hazard.status.value}，不可提交整改"}

    now = time.time()
    hazard.status = HazardStatus.PENDING_REVIEW
    hazard.rectification_remark = rectification_remark
    hazard.rectification_submitted_at = now
    hazard.updated_at = now
    return {"success": True, "message": "整改完成，待复查", "hazard": hazard}


def review_hazard(
    hazard_id: str,
    reviewer: str,
    passed: bool,
    review_remark: Optional[str] = None,
    reject_reason: Optional[str] = None,
) -> Dict:
    hazard = hazards.get(hazard_id)
    if not hazard:
        return {"error": f"隐患 {hazard_id} 不存在"}
    if hazard.status != HazardStatus.PENDING_REVIEW:
        return {"error": f"隐患当前状态为 {hazard.status.value}，不可复查"}

    now = time.time()
    if passed:
        hazard.status = HazardStatus.CLOSED
        hazard.review_result = True
        hazard.review_remark = review_remark or ""
        hazard.reviewed_at = now
        hazard.updated_at = now
        return {"success": True, "message": "复查通过，隐患已关闭", "hazard": hazard}
    else:
        if not reject_reason:
            return {"error": "复查不通过时必须填写打回原因"}
        hazard.status = HazardStatus.RECTIFYING
        hazard.review_result = False
        hazard.review_remark = review_remark or ""
        hazard.reject_reason = reject_reason
        hazard.reject_count += 1
        hazard.reviewed_at = now
        hazard.updated_at = now
        return {"success": True, "message": f"复查不通过，已打回整改。原因: {reject_reason}", "hazard": hazard}


def get_hazard(hazard_id: str) -> Optional[Hazard]:
    return hazards.get(hazard_id)


def get_hazards(
    crane_id: Optional[str] = None,
    status: Optional[HazardStatus] = None,
    severity: Optional[HazardSeverity] = None,
    include_closed: bool = True,
    is_overdue: Optional[bool] = None,
) -> List[Hazard]:
    result = list(hazards.values())
    if crane_id:
        result = [h for h in result if h.crane_id == crane_id]
    if status:
        result = [h for h in result if h.status == status]
    elif not include_closed:
        result = [h for h in result if h.status != HazardStatus.CLOSED]
    if severity:
        result = [h for h in result if h.severity == severity]
    if is_overdue is not None:
        result = [h for h in result if h.is_overdue == is_overdue]
    result.sort(key=lambda h: h.created_at, reverse=True)
    return result


def check_overdue_hazards() -> List[Hazard]:
    now = time.time()
    overdue_list: List[Hazard] = []
    for hazard in hazards.values():
        if hazard.status == HazardStatus.CLOSED:
            continue
        if now > hazard.deadline and not hazard.is_overdue:
            hazard.is_overdue = True
            hazard.updated_at = now
            overdue_list.append(hazard)
        elif hazard.is_overdue and now <= hazard.deadline:
            hazard.is_overdue = False
            hazard.updated_at = now
    return overdue_list


def get_crane_overdue_hazard_warnings(crane_id: str) -> List[Dict]:
    crane_hazards = get_hazards(crane_id=crane_id, include_closed=False)
    warnings = []
    for h in crane_hazards:
        if h.is_overdue:
            warnings.append({
                "hazard_id": h.hazard_id,
                "item_name": h.item_name,
                "severity": h.severity.value,
                "status": h.status.value,
                "deadline": h.deadline,
                "responsible_person": h.responsible_person,
                "overdue_hours": round((time.time() - h.deadline) / 3600, 1),
            })
    return warnings


def get_crane_hazard_stats(crane_id: Optional[str] = None) -> List[CraneHazardStats]:
    crane_ids = [crane_id] if crane_id else list(cranes_config.keys())
    stats_list: List[CraneHazardStats] = []

    for cid in crane_ids:
        config = cranes_config.get(cid)
        if not config:
            continue
        crane_hazards = get_hazards(crane_id=cid, include_closed=True)
        closed_durations: List[float] = []
        for h in crane_hazards:
            if h.status == HazardStatus.CLOSED and h.reviewed_at:
                closed_durations.append((h.reviewed_at - h.created_at) / 3600)

        avg_close = round(sum(closed_durations) / len(closed_durations), 1) if closed_durations else 0.0

        stats = CraneHazardStats(
            crane_id=cid,
            crane_name=config.name,
            total_hazards=len(crane_hazards),
            pending_count=sum(1 for h in crane_hazards if h.status == HazardStatus.PENDING_RECTIFICATION),
            rectifying_count=sum(1 for h in crane_hazards if h.status == HazardStatus.RECTIFYING),
            review_count=sum(1 for h in crane_hazards if h.status == HazardStatus.PENDING_REVIEW),
            closed_count=sum(1 for h in crane_hazards if h.status == HazardStatus.CLOSED),
            overdue_count=sum(1 for h in crane_hazards if h.is_overdue),
            avg_close_hours=avg_close,
        )
        stats_list.append(stats)
    return stats_list


def get_inspection_daily_stats(crane_id: str, start_ts: float, end_ts: float) -> InspectionDailyStats:
    stats = InspectionDailyStats()

    for report in inspection_reports.values():
        if report.crane_id != crane_id:
            continue
        if not (start_ts <= report.inspection_time < end_ts):
            continue
        if not stats.inspection_completed or report.inspection_time > (stats.inspection_time or 0):
            stats.inspection_completed = True
            stats.inspection_report_id = report.report_id
            stats.inspector = report.inspector
            stats.inspection_time = report.inspection_time
            stats.hazards_found = report.fail_count

    all_active_hazards = get_hazards(crane_id=crane_id, include_closed=False)
    stats.overdue_hazards = sum(1 for h in all_active_hazards if h.is_overdue)

    return stats
