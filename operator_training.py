import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from models import (
    Operator,
    OperatorCreate,
    OperatorUpdate,
    OperatorGrade,
    OperatorQualificationStatus,
    GRADE_ARM_LENGTH_LIMITS,
    ASSESSMENT_PASSING_SCORE,
    ASSESSMENT_VALIDITY_MONTHS,
    AssessmentRecord,
    AssessmentCreate,
    CraneOperatorBinding,
    ShiftHandoverRecord,
    OperatorAttendanceSegment,
)
from collision import cranes_config

operators: Dict[str, Operator] = {}
assessment_records: Dict[str, AssessmentRecord] = {}
operator_assessments: Dict[str, List[str]] = {}
crane_operator_bindings: Dict[str, CraneOperatorBinding] = {}
operator_crane_bindings: Dict[str, str] = {}
shift_handover_records: List[ShiftHandoverRecord] = []
attendance_segments: Dict[str, List[OperatorAttendanceSegment]] = {}


def init_operator_module():
    for crane_id in cranes_config.keys():
        if crane_id not in attendance_segments:
            attendance_segments[crane_id] = []


def _generate_operator_id() -> str:
    return f"OP-{uuid.uuid4().hex[:8].upper()}"


def _generate_assessment_id() -> str:
    return f"AS-{uuid.uuid4().hex[:8].upper()}"


def _generate_handover_id() -> str:
    return f"HO-{uuid.uuid4().hex[:8].upper()}"


def create_operator(create: OperatorCreate) -> Dict:
    now = time.time()
    operator_id = _generate_operator_id()
    operator = Operator(
        operator_id=operator_id,
        name=create.name,
        grade=create.grade,
        phone=create.phone,
        id_number=create.id_number,
        created_at=now,
        updated_at=now,
    )
    operators[operator_id] = operator
    operator_assessments[operator_id] = []
    return {
        "success": True,
        "operator": operator,
        "message": "操作员创建成功",
    }


def update_operator(operator_id: str, update: OperatorUpdate) -> Dict:
    operator = operators.get(operator_id)
    if not operator:
        return {"error": f"操作员 {operator_id} 不存在"}

    now = time.time()

    if operator_crane_bindings.get(operator_id):
        if update.grade is not None and update.grade != operator.grade:
            bound_crane_id = operator_crane_bindings[operator_id]
            config = cranes_config.get(bound_crane_id)
            if config:
                limit = GRADE_ARM_LENGTH_LIMITS.get(update.grade)
                if limit is not None and config.arm_length > limit:
                    return {
                        "error": (
                            f"操作员当前已绑定塔吊 {bound_crane_id} (臂长{config.arm_length}米), "
                            f"降级为{update.grade.value}后资质不足(上限{limit}米), 请先解绑再变更等级"
                        ),
                    }

    if update.name is not None:
        operator.name = update.name
    if update.grade is not None:
        operator.grade = update.grade
    if update.phone is not None:
        operator.phone = update.phone
    if update.id_number is not None:
        operator.id_number = update.id_number
    operator.updated_at = now

    return {
        "success": True,
        "operator": operator,
        "message": "操作员信息更新成功",
    }


def get_operator(operator_id: str) -> Optional[Operator]:
    return operators.get(operator_id)


def list_operators(grade: Optional[OperatorGrade] = None) -> List[Operator]:
    result = list(operators.values())
    if grade:
        result = [o for o in result if o.grade == grade]
    return result


def can_operator_operate_crane(operator_id: str, crane_id: str) -> Dict:
    operator = operators.get(operator_id)
    if not operator:
        return {"can_operate": False, "reason": f"操作员 {operator_id} 不存在"}

    config = cranes_config.get(crane_id)
    if not config:
        return {"can_operate": False, "reason": f"塔吊 {crane_id} 不存在"}

    limit = GRADE_ARM_LENGTH_LIMITS.get(operator.grade)
    if limit is not None and config.arm_length > limit:
        return {
            "can_operate": False,
            "reason": (
                f"操作员等级 {operator.grade.value} 仅可操作臂长不超过{limit}米的塔吊, "
                f"塔吊 {crane_id} 臂长为{config.arm_length}米"
            ),
        }

    qualification = get_operator_qualification(operator_id)
    if not qualification["is_qualified"]:
        status = qualification["status"]
        reasons = []
        if status.latest_assessment is None:
            reasons.append("尚无考核记录")
        elif not status.latest_assessment.passed:
            reasons.append(f"最近考核未及格(得分{status.latest_assessment.score})")
        elif status.qualification_expiry and time.time() > status.qualification_expiry:
            reasons.append("考核已过期, 需重新考核")
        return {
            "can_operate": False,
            "reason": f"操作员无有效作业资格: {'; '.join(reasons)}",
        }

    return {"can_operate": True, "reason": ""}


def record_assessment(operator_id: str, create: AssessmentCreate) -> Dict:
    operator = operators.get(operator_id)
    if not operator:
        return {"error": f"操作员 {operator_id} 不存在"}

    if create.score < 0 or create.score > 100:
        return {"error": "考核分数必须在0-100之间"}

    now = time.time()
    passed = create.score >= ASSESSMENT_PASSING_SCORE
    valid_until = None
    if passed:
        valid_until = now + ASSESSMENT_VALIDITY_MONTHS * 30 * 86400

    assessment_id = _generate_assessment_id()
    record = AssessmentRecord(
        assessment_id=assessment_id,
        operator_id=operator_id,
        score=create.score,
        passed=passed,
        assessed_at=now,
        valid_until=valid_until,
        assessor=create.assessor,
        remarks=create.remarks or "",
    )
    assessment_records[assessment_id] = record
    if operator_id not in operator_assessments:
        operator_assessments[operator_id] = []
    operator_assessments[operator_id].append(assessment_id)

    if not passed:
        bound_crane_id = operator_crane_bindings.get(operator_id)
        if bound_crane_id:
            _unbind_operator_from_crane_internal(operator_id, bound_crane_id, reason="考核未及格, 自动解绑")

    return {
        "success": True,
        "assessment": record,
        "message": f"考核记录已录入, {'及格' if passed else '未及格'}(及格线{ASSESSMENT_PASSING_SCORE}分)",
    }


def get_operator_assessments(operator_id: str) -> List[AssessmentRecord]:
    ids = operator_assessments.get(operator_id, [])
    return [assessment_records[aid] for aid in ids if aid in assessment_records]


def get_operator_qualification(operator_id: str) -> Dict:
    operator = operators.get(operator_id)
    if not operator:
        return {"is_qualified": False, "reason": f"操作员 {operator_id} 不存在"}

    assessments = get_operator_assessments(operator_id)
    latest_passed = None
    for a in reversed(assessments):
        if a.passed:
            latest_passed = a
            break

    is_qualified = False
    qualification_expiry = None

    if latest_passed:
        qualification_expiry = latest_passed.valid_until
        if qualification_expiry and time.time() <= qualification_expiry:
            is_qualified = True

    bound_crane_id = operator_crane_bindings.get(operator_id)
    arm_limit = GRADE_ARM_LENGTH_LIMITS.get(operator.grade)

    status = OperatorQualificationStatus(
        operator_id=operator_id,
        operator_name=operator.name,
        grade=operator.grade,
        is_qualified=is_qualified,
        latest_assessment=latest_passed,
        qualification_expiry=qualification_expiry,
        bound_crane_id=bound_crane_id,
        arm_length_limit=arm_limit,
    )

    return {
        "is_qualified": is_qualified,
        "status": status,
    }


def bind_operator_to_crane(crane_id: str, operator_id: str) -> Dict:
    if crane_id not in cranes_config:
        return {"error": f"塔吊 {crane_id} 不存在"}

    operator = operators.get(operator_id)
    if not operator:
        return {"error": f"操作员 {operator_id} 不存在"}

    current_binding = crane_operator_bindings.get(crane_id)
    if current_binding and current_binding.is_active:
        return {
            "error": (
                f"塔吊 {crane_id} 当前已绑定操作员 {current_binding.operator_id}, "
                f"请先解绑或执行换班交接"
            ),
        }

    existing_crane = operator_crane_bindings.get(operator_id)
    if existing_crane:
        return {
            "error": f"操作员 {operator_id} 当前已绑定塔吊 {existing_crane}, 请先解绑后再绑定新塔吊",
        }

    check = can_operator_operate_crane(operator_id, crane_id)
    if not check["can_operate"]:
        return {"error": check["reason"]}

    now = time.time()
    binding = CraneOperatorBinding(
        crane_id=crane_id,
        operator_id=operator_id,
        bound_at=now,
        is_active=True,
    )
    crane_operator_bindings[crane_id] = binding
    operator_crane_bindings[operator_id] = crane_id

    if crane_id not in attendance_segments:
        attendance_segments[crane_id] = []
    segment = OperatorAttendanceSegment(
        operator_id=operator_id,
        operator_name=operator.name,
        start_time=now,
        is_current=True,
    )
    attendance_segments[crane_id].append(segment)

    return {
        "success": True,
        "binding": binding,
        "message": f"操作员 {operator.name} 已绑定到塔吊 {crane_id}",
    }


def _unbind_operator_from_crane_internal(operator_id: str, crane_id: str, reason: str = ""):
    binding = crane_operator_bindings.get(crane_id)
    if binding and binding.is_active and binding.operator_id == operator_id:
        now = time.time()
        binding.is_active = False
        binding.unbound_at = now
        operator_crane_bindings.pop(operator_id, None)

        for seg in attendance_segments.get(crane_id, []):
            if seg.operator_id == operator_id and seg.is_current:
                seg.end_time = now
                seg.is_current = False


def unbind_operator_from_crane(crane_id: str, reason: str = "") -> Dict:
    binding = crane_operator_bindings.get(crane_id)
    if not binding or not binding.is_active:
        return {"error": f"塔吊 {crane_id} 当前无绑定操作员"}

    operator_id = binding.operator_id
    operator = operators.get(operator_id)
    operator_name = operator.name if operator else operator_id

    _unbind_operator_from_crane_internal(operator_id, crane_id, reason)

    return {
        "success": True,
        "message": f"操作员 {operator_name} 已从塔吊 {crane_id} 解绑",
    }


def shift_handover(crane_id: str, from_operator_id: str, to_operator_id: str, remarks: str = "") -> Dict:
    if crane_id not in cranes_config:
        return {"error": f"塔吊 {crane_id} 不存在"}

    from_operator = operators.get(from_operator_id)
    if not from_operator:
        return {"error": f"交班操作员 {from_operator_id} 不存在"}

    to_operator = operators.get(to_operator_id)
    if not to_operator:
        return {"error": f"接班操作员 {to_operator_id} 不存在"}

    if from_operator_id == to_operator_id:
        return {"error": "交班和接班操作员不能是同一人"}

    current_binding = crane_operator_bindings.get(crane_id)
    if not current_binding or not current_binding.is_active or current_binding.operator_id != from_operator_id:
        return {
            "error": f"操作员 {from_operator_id} 当前未绑定塔吊 {crane_id}, 无法进行交接",
        }

    check = can_operator_operate_crane(to_operator_id, crane_id)
    if not check["can_operate"]:
        return {"error": f"接班操作员无法操作该塔吊: {check['reason']}"}

    to_existing_crane = operator_crane_bindings.get(to_operator_id)
    if to_existing_crane:
        to_operator = operators.get(to_operator_id)
        to_name = to_operator.name if to_operator else to_operator_id
        return {
            "error": f"接班操作员 {to_name} 当前已绑定塔吊 {to_existing_crane}, 请先解绑后再进行交接",
        }

    has_pending_orders, pending_order_ids = _check_pending_orders(crane_id)
    has_unresolved_alarms, unresolved_alarm_ids = _check_unresolved_alarms(crane_id)

    if (has_pending_orders or has_unresolved_alarms) and (not remarks or not remarks.strip()):
        detail_parts = []
        if has_pending_orders:
            detail_parts.append(f"存在未完成工单({len(pending_order_ids)}个)")
        if has_unresolved_alarms:
            detail_parts.append(f"存在未解除告警({len(unresolved_alarm_ids)}个)")
        return {
            "error": f"该塔吊当前{'/'.join(detail_parts)}, 交接时必须在备注中说明交接原因",
        }

    now = time.time()
    handover_id = _generate_handover_id()
    record = ShiftHandoverRecord(
        handover_id=handover_id,
        crane_id=crane_id,
        from_operator_id=from_operator_id,
        to_operator_id=to_operator_id,
        from_operator_name=from_operator.name,
        to_operator_name=to_operator.name,
        has_pending_orders=has_pending_orders,
        has_unresolved_alarms=has_unresolved_alarms,
        pending_order_ids=pending_order_ids,
        unresolved_alarm_ids=unresolved_alarm_ids,
        remarks=remarks or "",
        handed_over_at=now,
    )
    shift_handover_records.append(record)

    _unbind_operator_from_crane_internal(from_operator_id, crane_id, reason="换班交接")

    binding = CraneOperatorBinding(
        crane_id=crane_id,
        operator_id=to_operator_id,
        bound_at=now,
        is_active=True,
    )
    crane_operator_bindings[crane_id] = binding
    operator_crane_bindings[to_operator_id] = crane_id

    if crane_id not in attendance_segments:
        attendance_segments[crane_id] = []
    segment = OperatorAttendanceSegment(
        operator_id=to_operator_id,
        operator_name=to_operator.name,
        start_time=now,
        is_current=True,
    )
    attendance_segments[crane_id].append(segment)

    return {
        "success": True,
        "handover": record,
        "message": f"换班交接完成: {from_operator.name} → {to_operator.name}",
    }


def _check_pending_orders(crane_id: str) -> tuple:
    try:
        from scheduler import work_orders
        from models import WorkOrderStatus
        pending = []
        for order in work_orders.values():
            if order.assigned_crane_id == crane_id and order.status in (
                WorkOrderStatus.ASSIGNED,
                WorkOrderStatus.EXECUTING,
            ):
                pending.append(order.order_id)
        return len(pending) > 0, pending
    except ImportError:
        return False, []


def _check_unresolved_alarms(crane_id: str) -> tuple:
    try:
        from collision import cranes_lock_status
        unresolved = []
        lock = cranes_lock_status.get(crane_id)
        if lock and lock.is_locked:
            unresolved.append(f"LOCK-{crane_id}-{lock.locked_reason or 'unknown'}")
        try:
            from anomaly_detector import cranes_freeze_status
            freeze = cranes_freeze_status.get(crane_id)
            if freeze and freeze.is_frozen:
                unresolved.append(f"FREEZE-{crane_id}-{freeze.frozen_reason or 'unknown'}")
        except ImportError:
            pass
        try:
            from anomaly_detector import cranes_anomaly_events
            for events in cranes_anomaly_events.values():
                for ev in events:
                    if not ev.resolved and (ev.crane_id == crane_id):
                        unresolved.append(ev.event_id)
        except ImportError:
            pass
        return len(unresolved) > 0, unresolved
    except ImportError:
        return False, []


def get_crane_attendance_timeline(crane_id: str, date: Optional[str] = None) -> Dict:
    if crane_id not in cranes_config:
        return {"error": f"塔吊 {crane_id} 不存在"}

    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    try:
        target_date = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return {"error": "日期格式错误, 应为 YYYY-MM-DD"}

    day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    day_end = target_date.replace(hour=23, minute=59, second=59, microsecond=999999).timestamp()

    segments = attendance_segments.get(crane_id, [])
    now = time.time()
    day_segments = []
    for seg in segments:
        if seg.end_time is None:
            if seg.start_time > day_end:
                continue
            if day_start > now:
                continue
            day_segments.append(seg)
        else:
            if seg.start_time <= day_end and seg.end_time >= day_start:
                day_segments.append(seg)

    return {
        "crane_id": crane_id,
        "date": date,
        "segments": day_segments,
        "total_segments": len(day_segments),
    }


def get_crane_current_operator(crane_id: str) -> Optional[Dict]:
    binding = crane_operator_bindings.get(crane_id)
    if not binding or not binding.is_active:
        return None
    operator = operators.get(binding.operator_id)
    if not operator:
        return None
    qualification = get_operator_qualification(binding.operator_id)
    return {
        "operator": operator,
        "binding": binding,
        "qualification": qualification.get("status"),
    }


def validate_crane_operator_for_status_report(crane_id: str) -> Optional[Dict]:
    binding = crane_operator_bindings.get(crane_id)
    if not binding or not binding.is_active:
        return {
            "valid": False,
            "reason": f"塔吊 {crane_id} 无操作员在岗, 拒绝状态上报",
        }

    operator_id = binding.operator_id
    check = can_operator_operate_crane(operator_id, crane_id)
    if not check["can_operate"]:
        return {
            "valid": False,
            "reason": check["reason"],
        }

    return {"valid": True}


def get_handover_history(crane_id: str, limit: int = 50) -> List[ShiftHandoverRecord]:
    records = [r for r in shift_handover_records if r.crane_id == crane_id]
    records.sort(key=lambda r: r.handed_over_at, reverse=True)
    return records[:limit]
