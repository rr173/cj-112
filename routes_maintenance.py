from typing import List, Optional

from fastapi import APIRouter, HTTPException

from models import (
    MaintenanceWindowCreate,
    MaintenanceConfirmRequest,
    MaintenanceStatus,
    MaintenanceType,
    MaintenanceAlarmType,
    MaintenanceWindowQuery,
)
from maintenance import (
    create_maintenance_window,
    confirm_maintenance_complete,
    get_maintenance_windows,
    get_maintenance_window,
    get_maintenance_records_by_window,
    get_maintenance_history,
    get_crane_maintenance_status,
    get_all_cranes_maintenance_status,
    get_active_maintenance_windows,
    get_maintenance_alarms,
    set_crane_maintenance_cycle,
    check_all_windows,
)

router = APIRouter(prefix="/api/maintenance", tags=["塔吊维保管理"])


@router.post("/windows", summary="创建维保停机窗口")
def api_create_maintenance_window(create: MaintenanceWindowCreate):
    check_all_windows()
    result = create_maintenance_window(create)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "data": result,
    }


@router.get("/windows", summary="查询维保计划列表(按塔吊和状态筛选)")
def api_list_maintenance_windows(
    crane_id: Optional[str] = None,
    status: Optional[MaintenanceStatus] = None,
    maintenance_type: Optional[MaintenanceType] = None,
    start_from: Optional[float] = None,
    end_before: Optional[float] = None,
    limit: int = 100,
):
    check_all_windows()
    query = MaintenanceWindowQuery(
        crane_id=crane_id,
        status=status,
        maintenance_type=maintenance_type,
        start_from=start_from,
        end_before=end_before,
    )
    windows = get_maintenance_windows(query)
    return {
        "code": 0,
        "total": len(windows),
        "windows": windows[:limit],
    }


@router.get("/windows/active", summary="查询当前停机中的塔吊列表")
def api_get_active_maintenance_windows():
    check_all_windows()
    windows = get_active_maintenance_windows()
    return {
        "code": 0,
        "total": len(windows),
        "active_windows": windows,
    }


@router.get("/windows/{window_id}", summary="查询单个维保窗口详情")
def api_get_maintenance_window(window_id: str):
    check_all_windows()
    window = get_maintenance_window(window_id)
    if not window:
        raise HTTPException(status_code=404, detail=f"维保窗口 {window_id} 不存在")
    records = get_maintenance_records_by_window(window_id)
    return {
        "code": 0,
        "window": window,
        "records": records,
    }


@router.post("/windows/{window_id}/confirm", summary="确认维保完成")
def api_confirm_maintenance_complete(window_id: str, confirm: MaintenanceConfirmRequest):
    check_all_windows()
    result = confirm_maintenance_complete(window_id, confirm)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "window": result["window"],
        "record": result["record"],
    }


@router.get("/cranes/status", summary="查询所有塔吊维保状态和下次维保倒计时")
def api_get_all_cranes_maintenance_status():
    check_all_windows()
    statuses = get_all_cranes_maintenance_status()
    return {
        "code": 0,
        "total": len(statuses),
        "statuses": statuses,
    }


@router.get("/cranes/{crane_id}/status", summary="查询单台塔吊维保状态和下次维保倒计时")
def api_get_crane_maintenance_status(crane_id: str):
    check_all_windows()
    status = get_crane_maintenance_status(crane_id)
    if not status:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    return {
        "code": 0,
        "status": status,
    }


@router.get("/cranes/{crane_id}/history", summary="查询单台塔吊维保历史")
def api_get_maintenance_history(crane_id: str, limit: int = 50):
    from collision import cranes_config
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    history = get_maintenance_history(crane_id)
    return {
        "code": 0,
        "crane_id": crane_id,
        "total": len(history),
        "history": history[:limit],
    }


@router.get("/alarms", summary="查询维保告警历史")
def api_get_maintenance_alarms(
    crane_id: Optional[str] = None,
    alarm_type: Optional[MaintenanceAlarmType] = None,
    limit: int = 100,
):
    alarms = get_maintenance_alarms(crane_id, alarm_type, limit)
    return {
        "code": 0,
        "total": len(alarms),
        "alarms": alarms,
    }


@router.post("/cranes/{crane_id}/cycle", summary="设置塔吊维保周期(天数)")
def api_set_maintenance_cycle(crane_id: str, cycle_days: int):
    result = set_crane_maintenance_cycle(crane_id, cycle_days)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": f"塔吊 {crane_id} 维保周期已设置为 {cycle_days} 天",
        "data": result,
    }
