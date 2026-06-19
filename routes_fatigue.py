from typing import Optional

from fastapi import APIRouter, HTTPException

from models import (
    FatigueLevel,
    FatigueThresholdUpdateRequest,
)
from fatigue_monitor import (
    get_operator_fatigue_status,
    get_all_operator_fatigue_status,
    get_fatigue_alarm_history,
    update_fatigue_config,
    get_global_fatigue_config,
    get_operator_fatigue_config,
    release_forced_shiftover,
    get_fatigue_stats,
)
from operator_training import operators

router = APIRouter(prefix="/api/fatigue", tags=["操作员疲劳度监测"])


@router.get("/config", summary="查询全局疲劳阈值配置")
def api_get_global_config():
    config = get_global_fatigue_config()
    return {
        "code": 0,
        "config": config,
    }


@router.get("/config/{operator_id}", summary="查询指定操作员的疲劳阈值配置")
def api_get_operator_config(operator_id: str):
    if operator_id not in operators:
        raise HTTPException(status_code=404, detail=f"操作员 {operator_id} 不存在")
    config = get_operator_fatigue_config(operator_id)
    return {
        "code": 0,
        "operator_id": operator_id,
        "config": config,
    }


@router.put("/config", summary="更新疲劳阈值配置（热更新）")
def api_update_config(update: FatigueThresholdUpdateRequest, operator_id: Optional[str] = None):
    try:
        result = update_fatigue_config(
            operator_id=operator_id,
            mild_fatigue_hours=update.mild_fatigue_hours,
            severe_fatigue_hours=update.severe_fatigue_hours,
            forced_shiftover_hours=update.forced_shiftover_hours,
            rest_reset_minutes=update.rest_reset_minutes,
        )
        return {
            "code": 0,
            "message": "疲劳阈值配置更新成功",
            "updated_target": result["updated_target"],
            "current_config": result["current_config"],
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/operators/{operator_id}", summary="查询操作员当前疲劳状态")
def api_get_operator_fatigue(operator_id: str):
    if operator_id not in operators:
        raise HTTPException(status_code=404, detail=f"操作员 {operator_id} 不存在")
    status = get_operator_fatigue_status(operator_id)
    if not status:
        raise HTTPException(status_code=404, detail=f"操作员 {operator_id} 疲劳状态不存在")
    return {
        "code": 0,
        "fatigue_status": status,
    }


@router.get("/operators", summary="查询所有操作员当前疲劳状态")
def api_get_all_fatigue_status():
    statuses = get_all_operator_fatigue_status()
    return {
        "code": 0,
        "total": len(statuses),
        "fatigue_statuses": statuses,
    }


@router.get("/events", summary="查询疲劳事件历史")
def api_get_fatigue_events(
    operator_id: Optional[str] = None,
    crane_id: Optional[str] = None,
    alarm_level: Optional[FatigueLevel] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    limit: int = 200,
):
    events = get_fatigue_alarm_history(
        operator_id=operator_id,
        crane_id=crane_id,
        alarm_level=alarm_level,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
    )
    return {
        "code": 0,
        "total": len(events),
        "events": events,
    }


@router.post("/operators/{operator_id}/release-forced-shiftover", summary="手动解除操作员强制换班状态")
def api_release_forced_shiftover(operator_id: str):
    if operator_id not in operators:
        raise HTTPException(status_code=404, detail=f"操作员 {operator_id} 不存在")
    result = release_forced_shiftover(operator_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "operator_id": result["operator_id"],
        "crane_id": result["crane_id"],
    }


@router.get("/stats", summary="查询疲劳度监测统计数据")
def api_get_fatigue_stats():
    stats = get_fatigue_stats()
    return {
        "code": 0,
        "stats": stats,
    }
