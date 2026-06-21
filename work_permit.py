import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from models import (
    WorkPermit,
    WorkPermitCheckItem,
    WorkPermitStatus,
    WorkPermitExtension,
    WorkPermitExtensionStatus,
    WorkPermitCheckResult,
    WorkPermitDailyStats,
    AlarmType,
)
from collision import cranes_config, alarm_history


work_permits: Dict[str, WorkPermit] = {}
crane_active_permit: Dict[str, str] = {}


def _generate_permit_id() -> str:
    return f"WP-{uuid.uuid4().hex[:12].upper()}"


def _generate_extension_id() -> str:
    return f"WPE-{uuid.uuid4().hex[:8].upper()}"


def _get_date_str(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _get_crane_name(crane_id: str) -> str:
    config = cranes_config.get(crane_id)
    return config.name if config else crane_id


def _log_work_permit_alarm(
    crane_id: str,
    alarm_type: AlarmType,
    message: str,
    details: Optional[Dict] = None,
):
    try:
        from models import AlarmEvent
        now = time.time()
        alarm = AlarmEvent(
            alarm_id=f"WPA-{uuid.uuid4().hex[:10].upper()}",
            alarm_type=alarm_type,
            timestamp=now,
            datetime_str=datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
            crane_a_id=crane_id,
            crane_b_id="",
            message=message,
            details=details or {},
        )
        alarm_history.append(alarm)
    except Exception as e:
        print(f"[作业许可] 写入告警历史失败: {e}")


def _get_end_of_day(ts: float) -> float:
    dt = datetime.fromtimestamp(ts)
    end_of_day = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return end_of_day.timestamp()


def _get_23_30_of_day(ts: float) -> float:
    dt = datetime.fromtimestamp(ts)
    t2330 = dt.replace(hour=23, minute=30, second=0, microsecond=0)
    return t2330.timestamp()


def _get_01_00_next_day(ts: float) -> float:
    dt = datetime.fromtimestamp(ts) + timedelta(days=1)
    t0100 = dt.replace(hour=1, minute=0, second=0, microsecond=0)
    return t0100.timestamp()


def _check_operator_qualified(crane_id: str) -> WorkPermitCheckResult:
    try:
        from operator_training import get_crane_current_operator, can_operator_operate_crane

        current_op = get_crane_current_operator(crane_id)
        if not current_op:
            return WorkPermitCheckResult(
                check_item=WorkPermitCheckItem.OPERATOR_QUALIFIED,
                passed=False,
                message="塔吊无操作员在岗",
                details={"crane_id": crane_id},
            )

        op = current_op["operator"]
        check = can_operator_operate_crane(op.operator_id, crane_id)
        if not check["can_operate"]:
            return WorkPermitCheckResult(
                check_item=WorkPermitCheckItem.OPERATOR_QUALIFIED,
                passed=False,
                message=check["reason"],
                details={
                    "crane_id": crane_id,
                    "operator_id": op.operator_id,
                    "operator_name": op.name,
                },
            )

        return WorkPermitCheckResult(
            check_item=WorkPermitCheckItem.OPERATOR_QUALIFIED,
            passed=True,
            message="操作员在岗且资质有效",
            details={
                "crane_id": crane_id,
                "operator_id": op.operator_id,
                "operator_name": op.name,
            },
        )
    except ImportError:
        return WorkPermitCheckResult(
            check_item=WorkPermitCheckItem.OPERATOR_QUALIFIED,
            passed=False,
            message="操作员模块未加载，无法检查",
            details={"crane_id": crane_id},
        )


def _check_inspection_completed(crane_id: str) -> WorkPermitCheckResult:
    try:
        from inspection import get_inspection_history, get_hazards, HazardSeverity, HazardStatus
        from models import HazardSeverity as HS

        today_str = _get_date_str(time.time())
        history = get_inspection_history(crane_id=crane_id, start_date=today_str, end_date=today_str)
        if not history:
            return WorkPermitCheckResult(
                check_item=WorkPermitCheckItem.INSPECTION_COMPLETED,
                passed=False,
                message="今日巡检尚未完成",
                details={"crane_id": crane_id},
            )

        latest_report = history[0]
        active_hazards = get_hazards(crane_id=crane_id, include_closed=False)
        severe_unclosed = [
            h for h in active_hazards
            if h.severity in (HS.HIGH, HS.CRITICAL) and h.status != HazardStatus.CLOSED
        ]
        if severe_unclosed:
            return WorkPermitCheckResult(
                check_item=WorkPermitCheckItem.INSPECTION_COMPLETED,
                passed=False,
                message=f"存在 {len(severe_unclosed)} 条未关闭的严重/紧急隐患",
                details={
                    "crane_id": crane_id,
                    "unclosed_count": len(severe_unclosed),
                    "hazard_ids": [h.hazard_id for h in severe_unclosed],
                },
            )

        return WorkPermitCheckResult(
            check_item=WorkPermitCheckItem.INSPECTION_COMPLETED,
            passed=True,
            message="今日巡检已完成且无未关闭的严重隐患",
            details={
                "crane_id": crane_id,
                "report_id": latest_report.report_id,
                "inspector": latest_report.inspector,
            },
        )
    except ImportError:
        return WorkPermitCheckResult(
            check_item=WorkPermitCheckItem.INSPECTION_COMPLETED,
            passed=False,
            message="巡检模块未加载，无法检查",
            details={"crane_id": crane_id},
        )


def _check_no_maintenance_window(crane_id: str) -> WorkPermitCheckResult:
    try:
        from maintenance import is_crane_in_maintenance, get_crane_current_window

        if is_crane_in_maintenance(crane_id):
            window = get_crane_current_window(crane_id)
            details = {"crane_id": crane_id}
            if window:
                details.update({
                    "window_id": window.window_id,
                    "start_time": window.start_time,
                    "end_time": window.end_time,
                    "maintenance_type": window.maintenance_type.value,
                })
            return WorkPermitCheckResult(
                check_item=WorkPermitCheckItem.NO_MAINTENANCE_WINDOW,
                passed=False,
                message="塔吊处于维保停机窗口内",
                details=details,
            )

        return WorkPermitCheckResult(
            check_item=WorkPermitCheckItem.NO_MAINTENANCE_WINDOW,
            passed=True,
            message="当前不在维保停机窗口内",
            details={"crane_id": crane_id},
        )
    except ImportError:
        return WorkPermitCheckResult(
            check_item=WorkPermitCheckItem.NO_MAINTENANCE_WINDOW,
            passed=False,
            message="维保模块未加载，无法检查",
            details={"crane_id": crane_id},
        )


def _check_no_forced_shiftover(crane_id: str) -> WorkPermitCheckResult:
    try:
        from operator_training import crane_operator_bindings
        from fatigue_monitor import is_operator_forced_shiftover

        binding = crane_operator_bindings.get(crane_id)
        if binding and binding.is_active:
            if is_operator_forced_shiftover(binding.operator_id):
                return WorkPermitCheckResult(
                    check_item=WorkPermitCheckItem.NO_FORCED_SHIFTOVER,
                    passed=False,
                    message="操作员处于强制换班锁定状态，需完成换班后方可作业",
                    details={
                        "crane_id": crane_id,
                        "operator_id": binding.operator_id,
                    },
                )

        return WorkPermitCheckResult(
            check_item=WorkPermitCheckItem.NO_FORCED_SHIFTOVER,
            passed=True,
            message="当前无未解除的强制换班锁定",
            details={"crane_id": crane_id},
        )
    except ImportError:
        return WorkPermitCheckResult(
            check_item=WorkPermitCheckItem.NO_FORCED_SHIFTOVER,
            passed=False,
            message="疲劳监控模块未加载，无法检查",
            details={"crane_id": crane_id},
        )


def _check_no_wind_shutdown(crane_id: str) -> WorkPermitCheckResult:
    try:
        from wind_speed_monitor import is_crane_wind_shutdown, get_crane_wind_shutdown_info

        if is_crane_wind_shutdown(crane_id):
            info = get_crane_wind_shutdown_info(crane_id)
            return WorkPermitCheckResult(
                check_item=WorkPermitCheckItem.NO_WIND_SHUTDOWN,
                passed=False,
                message="风速处于停机状态",
                details={
                    "crane_id": crane_id,
                    "shutdown_at": info.get("shutdown_at") if info else None,
                    "shutdown_reason": info.get("shutdown_reason") if info else None,
                },
            )

        return WorkPermitCheckResult(
            check_item=WorkPermitCheckItem.NO_WIND_SHUTDOWN,
            passed=True,
            message="风速正常，未处于停机状态",
            details={"crane_id": crane_id},
        )
    except ImportError:
        return WorkPermitCheckResult(
            check_item=WorkPermitCheckItem.NO_WIND_SHUTDOWN,
            passed=False,
            message="风速监测模块未加载，无法检查",
            details={"crane_id": crane_id},
        )


def _run_all_checks(crane_id: str) -> Tuple[List[WorkPermitCheckResult], bool]:
    check_functions = [
        _check_operator_qualified,
        _check_inspection_completed,
        _check_no_maintenance_window,
        _check_no_forced_shiftover,
        _check_no_wind_shutdown,
    ]
    results = []
    all_passed = True
    for check_func in check_functions:
        result = check_func(crane_id)
        results.append(result)
        if not result.passed:
            all_passed = False
    return results, all_passed


def _expire_outdated_permits():
    now = time.time()
    for permit_id in list(work_permits.keys()):
        permit = work_permits[permit_id]
        if permit.status == WorkPermitStatus.ACTIVE and now > permit.expires_at:
            permit.status = WorkPermitStatus.EXPIRED
            if crane_active_permit.get(permit.crane_id) == permit_id:
                del crane_active_permit[permit.crane_id]
            _log_work_permit_alarm(
                permit.crane_id,
                AlarmType.WORK_PERMIT_EXPIRED,
                f"塔吊 {_get_crane_name(permit.crane_id)} 作业许可证已过期",
                details={
                    "permit_id": permit_id,
                    "expires_at": permit.expires_at,
                    "permit_date": permit.permit_date,
                },
            )
            print(f"[作业许可] 塔吊 {permit.crane_id} 许可证 {permit_id} 已过期")


def init_work_permit_module():
    print("[作业许可模块] 初始化完成")


def apply_work_permit(crane_id: str) -> Dict:
    if crane_id not in cranes_config:
        return {"error": f"塔吊 {crane_id} 不存在"}

    _expire_outdated_permits()

    existing_active_id = crane_active_permit.get(crane_id)
    if existing_active_id:
        existing = work_permits.get(existing_active_id)
        if existing:
            return {
                "error": f"塔吊 {crane_id} 已持有有效的作业许可证",
                "existing_permit": existing,
            }

    check_results, all_passed = _run_all_checks(crane_id)
    now = time.time()

    permit_id = _generate_permit_id()
    permit_date = _get_date_str(now)
    expires_at = _get_end_of_day(now)

    operator_id = None
    operator_name = None
    for res in check_results:
        if res.check_item == WorkPermitCheckItem.OPERATOR_QUALIFIED and res.passed:
            operator_id = res.details.get("operator_id")
            operator_name = res.details.get("operator_name")
            break

    status = WorkPermitStatus.ACTIVE if all_passed else WorkPermitStatus.REVOKED

    permit = WorkPermit(
        permit_id=permit_id,
        crane_id=crane_id,
        crane_name=_get_crane_name(crane_id),
        status=status,
        issued_at=now,
        expires_at=expires_at,
        check_results=check_results,
        all_passed=all_passed,
        operator_id=operator_id,
        operator_name=operator_name,
        permit_date=permit_date,
    )

    if all_passed:
        work_permits[permit_id] = permit
        crane_active_permit[crane_id] = permit_id

        _log_work_permit_alarm(
            crane_id,
            AlarmType.WORK_PERMIT_ISSUED,
            f"塔吊 {_get_crane_name(crane_id)} 作业许可证已发放，有效期至 {datetime.fromtimestamp(expires_at).strftime('%Y-%m-%d %H:%M:%S')}",
            details={
                "permit_id": permit_id,
                "operator_id": operator_id,
                "operator_name": operator_name,
                "expires_at": expires_at,
            },
        )
        print(f"[作业许可] 塔吊 {crane_id} 许可证 {permit_id} 已发放，有效期至 {datetime.fromtimestamp(expires_at).strftime('%Y-%m-%d %H:%M:%S')}")

        return {
            "success": True,
            "message": "作业许可证发放成功",
            "permit": permit,
        }
    else:
        failed_items = [res for res in check_results if not res.passed]
        return {
            "success": False,
            "message": f"作业许可证申请被拒绝，{len(failed_items)} 项检查未通过",
            "check_results": check_results,
            "failed_items": failed_items,
        }


def get_crane_current_permit(crane_id: str) -> Optional[WorkPermit]:
    _expire_outdated_permits()
    check_and_revoke_if_needed(crane_id)
    permit_id = crane_active_permit.get(crane_id)
    if not permit_id:
        return None
    permit = work_permits.get(permit_id)
    if permit and permit.status == WorkPermitStatus.ACTIVE and time.time() <= permit.expires_at:
        return permit
    return None


def has_valid_permit(crane_id: str) -> bool:
    permit = get_crane_current_permit(crane_id)
    return permit is not None


def check_permit_and_get_rejection(crane_id: str) -> Optional[Dict]:
    permit = get_crane_current_permit(crane_id)
    if permit:
        return None
    return {
        "code": "NO_VALID_WORK_PERMIT",
        "message": f"塔吊 {crane_id} 无有效的当日作业许可证，请先申请作业许可",
        "crane_id": crane_id,
        "hint": "请调用 POST /api/work-permit/apply 申请当日作业许可证",
    }


def _revoke_permit_internal(
    permit_id: str,
    reason: str,
    revoked_by: WorkPermitCheckItem,
) -> Optional[WorkPermit]:
    permit = work_permits.get(permit_id)
    if not permit or permit.status != WorkPermitStatus.ACTIVE:
        return None

    now = time.time()
    permit.status = WorkPermitStatus.REVOKED
    permit.revoked_at = now
    permit.revoke_reason = reason
    permit.revoked_by_condition = revoked_by

    if crane_active_permit.get(permit.crane_id) == permit_id:
        del crane_active_permit[permit.crane_id]

    _log_work_permit_alarm(
        permit.crane_id,
        AlarmType.WORK_PERMIT_REVOKED,
        f"塔吊 {permit.crane_name} 作业许可证被吊销: {reason}",
        details={
            "permit_id": permit_id,
            "revoked_by": revoked_by.value,
            "revoked_at": now,
            "reason": reason,
        },
    )
    print(f"[作业许可] 塔吊 {permit.crane_id} 许可证 {permit_id} 被吊销，原因: {reason}")
    return permit


def check_and_revoke_if_needed(crane_id: str) -> Optional[WorkPermit]:
    permit = get_crane_current_permit(crane_id)
    if not permit:
        return None

    check_results, all_passed = _run_all_checks(crane_id)
    if all_passed:
        return None

    for res in check_results:
        if not res.passed:
            revoked = _revoke_permit_internal(
                permit.permit_id,
                res.message,
                res.check_item,
            )
            if revoked:
                return revoked
            break
    return None


def request_extension(crane_id: str, requested_by: str, requested_expiry: float) -> Dict:
    permit = get_crane_current_permit(crane_id)
    if not permit:
        return {"error": f"塔吊 {crane_id} 无有效的作业许可证"}

    if permit.extension and permit.extension.status != WorkPermitExtensionStatus.REJECTED:
        return {"error": f"该许可证已申请过延期，不可重复申请"}

    now = time.time()
    t2330 = _get_23_30_of_day(now)
    t0100_next = _get_01_00_next_day(now)

    if now < t2330:
        return {"error": "延期申请需在当日 23:30 之后提交"}

    if requested_expiry > t0100_next:
        return {"error": f"延期时间不得超过次日 01:00 ({t0100_next})"}

    if requested_expiry <= permit.expires_at:
        return {"error": "申请的延期时间必须晚于当前许可证到期时间"}

    try:
        from scheduler import work_orders, WorkOrderStatus
        has_executing_order = any(
            order.assigned_crane_id == crane_id and order.status == WorkOrderStatus.EXECUTING
            for order in work_orders.values()
        )
        if not has_executing_order:
            return {"error": "当前无正在执行的工单，无需申请延期"}
    except ImportError:
        pass

    extension = WorkPermitExtension(
        extension_id=_generate_extension_id(),
        requested_at=now,
        requested_by=requested_by,
        requested_expiry=requested_expiry,
        status=WorkPermitExtensionStatus.PENDING,
    )
    permit.extension = extension

    _log_work_permit_alarm(
        crane_id,
        AlarmType.WORK_PERMIT_EXTENSION_REQUESTED,
        f"塔吊 {_get_crane_name(crane_id)} 申请许可证延期至 {datetime.fromtimestamp(requested_expiry).strftime('%Y-%m-%d %H:%M:%S')}",
        details={
            "permit_id": permit.permit_id,
            "extension_id": extension.extension_id,
            "requested_by": requested_by,
            "requested_expiry": requested_expiry,
        },
    )

    return {
        "success": True,
        "message": "延期申请已提交，等待安全员审批",
        "extension": extension,
        "permit": permit,
    }


def approve_extension(
    permit_id: str,
    approved: bool,
    reviewed_by: str,
    review_remark: str = "",
    approved_expiry: Optional[float] = None,
) -> Dict:
    permit = work_permits.get(permit_id)
    if not permit:
        return {"error": f"作业许可证 {permit_id} 不存在"}

    if not permit.extension or permit.extension.status != WorkPermitExtensionStatus.PENDING:
        return {"error": "该许可证无待审批的延期申请"}

    now = time.time()
    extension = permit.extension
    extension.reviewed_at = now
    extension.reviewed_by = reviewed_by
    extension.review_remark = review_remark

    if approved:
        final_expiry = approved_expiry if approved_expiry else extension.requested_expiry
        t0100_next = _get_01_00_next_day(now)
        if final_expiry > t0100_next:
            return {"error": f"批准的延期时间不得超过次日 01:00 ({t0100_next})"}
        if final_expiry <= permit.expires_at:
            return {"error": "批准的延期时间必须晚于当前许可证到期时间"}

        extension.status = WorkPermitExtensionStatus.APPROVED
        permit.expires_at = final_expiry

        _log_work_permit_alarm(
            permit.crane_id,
            AlarmType.WORK_PERMIT_EXTENSION_APPROVED,
            f"塔吊 {permit.crane_name} 许可证延期已批准，有效期延长至 {datetime.fromtimestamp(final_expiry).strftime('%Y-%m-%d %H:%M:%S')}",
            details={
                "permit_id": permit_id,
                "extension_id": extension.extension_id,
                "approved_expiry": final_expiry,
                "reviewed_by": reviewed_by,
            },
        )
        return {
            "success": True,
            "message": f"延期已批准，许可证有效期延长至 {datetime.fromtimestamp(final_expiry).strftime('%Y-%m-%d %H:%M:%S')}",
            "extension": extension,
            "permit": permit,
        }
    else:
        extension.status = WorkPermitExtensionStatus.REJECTED
        _log_work_permit_alarm(
            permit.crane_id,
            AlarmType.WORK_PERMIT_EXTENSION_REJECTED,
            f"塔吊 {permit.crane_name} 许可证延期申请被拒绝",
            details={
                "permit_id": permit_id,
                "extension_id": extension.extension_id,
                "reviewed_by": reviewed_by,
                "review_remark": review_remark,
            },
        )
        return {
            "success": True,
            "message": "延期申请已拒绝",
            "extension": extension,
            "permit": permit,
        }


def check_and_expire_extension_on_order_complete(crane_id: str):
    permit = get_crane_current_permit(crane_id)
    if not permit or not permit.extension or permit.extension.status != WorkPermitExtensionStatus.APPROVED:
        return

    try:
        from scheduler import work_orders, WorkOrderStatus
        has_executing_order = any(
            order.assigned_crane_id == crane_id and order.status == WorkOrderStatus.EXECUTING
            for order in work_orders.values()
        )
        if not has_executing_order:
            now = time.time()
            permit.expires_at = now
            permit.status = WorkPermitStatus.EXPIRED
            if crane_active_permit.get(crane_id) == permit.permit_id:
                del crane_active_permit[crane_id]
            _log_work_permit_alarm(
                crane_id,
                AlarmType.WORK_PERMIT_EXPIRED,
                f"塔吊 {permit.crane_name} 延期许可证因工单完成已自动失效",
                details={
                    "permit_id": permit.permit_id,
                    "reason": "work_order_completed",
                },
            )
            print(f"[作业许可] 塔吊 {crane_id} 延期许可证因工单完成已自动失效")
    except ImportError:
        pass


def get_permit_status(crane_id: str) -> Dict:
    _expire_outdated_permits()

    permit = get_crane_current_permit(crane_id)
    if permit:
        check_results, all_passed = _run_all_checks(crane_id)
        return {
            "has_valid_permit": True,
            "permit": permit,
            "current_checks": check_results,
            "current_all_passed": all_passed,
        }

    pending_active_id = crane_active_permit.get(crane_id)
    pending_permit = work_permits.get(pending_active_id) if pending_active_id else None

    check_results, all_passed = _run_all_checks(crane_id)

    return {
        "has_valid_permit": False,
        "permit": pending_permit,
        "current_checks": check_results,
        "current_all_passed": all_passed,
        "message": "当前无有效的作业许可证，请申请许可",
    }


def get_permit_history(
    crane_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    status: Optional[WorkPermitStatus] = None,
    limit: int = 200,
) -> List[WorkPermit]:
    _expire_outdated_permits()

    results = list(work_permits.values())

    if crane_id:
        results = [p for p in results if p.crane_id == crane_id]
    if status:
        results = [p for p in results if p.status == status]
    if start_date:
        results = [p for p in results if p.permit_date >= start_date]
    if end_date:
        results = [p for p in results if p.permit_date <= end_date]

    results.sort(key=lambda p: p.issued_at, reverse=True)
    return results[:limit]


def get_permit(permit_id: str) -> Optional[WorkPermit]:
    _expire_outdated_permits()
    return work_permits.get(permit_id)


def get_work_permit_daily_stats(crane_id: str, start_ts: float, end_ts: float) -> WorkPermitDailyStats:
    stats = WorkPermitDailyStats()

    day_permits = [
        p for p in work_permits.values()
        if p.crane_id == crane_id and start_ts <= p.issued_at < end_ts
    ]

    if day_permits:
        issued = [p for p in day_permits if p.all_passed]
        if issued:
            latest = max(issued, key=lambda p: p.issued_at)
            stats.permit_issued = True
            stats.permit_id = latest.permit_id
            stats.issued_at = latest.issued_at
            if latest.extension:
                stats.extension_applied = True
                if latest.extension.status == WorkPermitExtensionStatus.APPROVED:
                    stats.extension_approved = True
                    stats.extended_expiry = latest.expires_at

        stats.revoke_count = sum(
            1 for p in day_permits
            if p.status == WorkPermitStatus.REVOKED
        )

    return stats
