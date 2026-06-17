import time
from typing import List, Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks

from models import CraneStatus, CraneFullStatus, AlarmType
from collision import (
    cranes_config,
    cranes_current_status,
    cranes_lock_status,
    alarm_history,
    pair_safety_thresholds,
    DEFAULT_SAFETY_THRESHOLD,
    normalize_angle,
    compute_crane_coords,
    check_collision_between,
    lock_crane,
    unlock_crane_record,
)
from arbiter import (
    cranes_held_tokens,
    clean_expired_tokens_and_waiters,
    find_sectors_for_crane_and_angle,
    get_crane_interval_for_sector,
    log_arb_event,
    EventType,
)
from anomaly_detector import (
    process_status_report_async,
    is_crane_frozen,
    cranes_freeze_status,
    add_status_to_window,
)
from models import CraneStatusRecord
from daily_report import add_status_report_to_history

router = APIRouter(prefix="/api", tags=["塔吊状态"])


@router.post("/crane/status", summary="上报塔吊状态(每秒一次)")
def report_crane_status(status: CraneStatus, background_tasks: BackgroundTasks):
    if status.crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {status.crane_id} 不存在")

    try:
        from maintenance import check_all_windows, is_crane_in_maintenance, increment_suppressed_alarm
        check_all_windows()
        in_maintenance = is_crane_in_maintenance(status.crane_id)
    except ImportError:
        in_maintenance = False

    if not in_maintenance:
        try:
            from operator_training import validate_crane_operator_for_status_report
            operator_check = validate_crane_operator_for_status_report(status.crane_id)
            if operator_check and not operator_check.get("valid"):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "message": operator_check["reason"],
                        "code": "NO_QUALIFIED_OPERATOR",
                        "crane_id": status.crane_id,
                    }
                )
        except ImportError:
            pass

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

    if is_crane_frozen(status.crane_id):
        freeze = cranes_freeze_status[status.crane_id]
        raise HTTPException(
            status_code=429,
            detail={
                "message": "塔吊处于异常检测冻结状态，暂时拒绝状态上报",
                "crane_id": status.crane_id,
                "frozen_at": freeze.frozen_at,
                "frozen_reason": freeze.frozen_reason,
                "unfreeze_at": freeze.unfreeze_at,
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

    status.timestamp = status.timestamp or time.time()
    cranes_current_status[status.crane_id] = status

    history_record = CraneStatusRecord(
        crane_id=status.crane_id,
        rotation_angle=status.rotation_angle,
        trolley_position=status.trolley_position,
        hook_height=status.hook_height,
        timestamp=status.timestamp,
    )
    add_status_report_to_history(history_record)
    add_status_to_window(status)

    if in_maintenance:
        try:
            from inspection import check_overdue_hazards, get_crane_overdue_hazard_warnings
            check_overdue_hazards()
            overdue_warnings = get_crane_overdue_hazard_warnings(status.crane_id)
        except ImportError:
            overdue_warnings = []

        response = {
            "code": 0,
            "message": "状态上报成功（维保停机模式：已记录，已跳过碰撞检测和异常检测）",
            "crane_id": status.crane_id,
            "maintenance_mode": True,
            "locked": cranes_lock_status[status.crane_id].is_locked if status.crane_id in cranes_lock_status else False,
            "overlap_sectors_entered": [],
            "alarms_triggered": 0,
            "alarm_details": [],
        }
        if overdue_warnings:
            response["overdue_hazard_warnings"] = overdue_warnings
            response["message"] = f"状态上报成功（维保停机模式：已记录，已跳过碰撞检测和异常检测）。注意：存在 {len(overdue_warnings)} 条超期未关闭隐患"
        return response

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

    background_tasks.add_task(process_status_report_async, status)

    triggered_alarms: List = []
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

    try:
        from inspection import check_overdue_hazards, get_crane_overdue_hazard_warnings
        check_overdue_hazards()
        overdue_warnings = get_crane_overdue_hazard_warnings(status.crane_id)
    except ImportError:
        overdue_warnings = []

    message = "状态上报成功"
    if overdue_warnings:
        message = f"状态上报成功。注意：存在 {len(overdue_warnings)} 条超期未关闭隐患"

    response = {
        "code": 0,
        "message": message,
        "crane_id": status.crane_id,
        "maintenance_mode": False,
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
    if overdue_warnings:
        response["overdue_hazard_warnings"] = overdue_warnings
    return response


@router.post("/crane/{crane_id}/unlock", summary="人工解除塔吊锁定")
def unlock_crane(crane_id: str):
    if crane_id not in cranes_lock_status:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    lock = cranes_lock_status[crane_id]
    was_locked = lock.is_locked
    prev_reason = lock.locked_reason
    if was_locked:
        unlock_crane_record(crane_id)
    lock.is_locked = False
    lock.locked_reason = None
    lock.locked_at = None
    return {
        "code": 0,
        "message": "锁定已解除" if was_locked else "塔吊本来就处于未锁定状态",
        "crane_id": crane_id,
        "previous_lock_reason": prev_reason,
    }


@router.get("/crane/{crane_id}/status", summary="查询塔吊实时状态")
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


@router.get("/cranes", summary="查询所有塔吊列表及状态")
def get_all_cranes():
    result = []
    for cid in cranes_config.keys():
        try:
            config = cranes_config[cid]
            status = cranes_current_status.get(cid)
            lock = cranes_lock_status[cid]

            arm_end = None
            hook = None
            swing = None
            if status:
                snap = compute_crane_coords(cid, status)
                if snap:
                    arm_end = {"x": snap.arm_end_x, "y": snap.arm_end_y, "z": snap.arm_end_z}
                    hook = {"x": snap.hook_x, "y": snap.hook_y, "z": snap.hook_z}
                    swing = snap.swing_radius

            result.append(CraneFullStatus(
                config=config,
                current_status=status,
                lock_status=lock,
                arm_end_coords=arm_end,
                hook_coords=hook,
                swing_radius=swing,
            ))
        except Exception:
            pass
    return result


@router.get("/alarms", summary="查询告警历史")
def get_alarm_history(
    crane_id: Optional[str] = None,
    alarm_type: Optional[AlarmType] = None,
    limit: int = 100
):
    result = alarm_history
    if crane_id:
        result = [a for a in result if a.crane_a_id == crane_id or a.crane_b_id == crane_id]
    if alarm_type:
        result = [a for a in result if a.alarm_type == alarm_type]
    return result[-limit:]


@router.get("/locks", summary="查询所有塔吊当前锁定状态")
def get_all_lock_status():
    return list(cranes_lock_status.values())


@router.get("/thresholds", summary="查询各塔吊对之间的安全间距阈值")
def get_thresholds():
    from collision import get_pair_key
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
