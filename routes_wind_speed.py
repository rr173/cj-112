from typing import Optional

from fastapi import APIRouter, HTTPException

from models import (
    WindSpeedReport,
    WindSpeedThresholdUpdateRequest,
    WindAlarmLevel,
)
from wind_speed_monitor import (
    process_wind_speed_report,
    get_wind_speed_status,
    get_all_wind_speed_statuses,
    get_wind_speed_history,
    get_wind_alarm_history,
    get_wind_recovery_history,
    manual_release_wind_shutdown,
    update_wind_config,
    get_crane_wind_config,
    get_global_wind_config,
    get_wind_stats,
)
from collision import cranes_config

router = APIRouter(prefix="/api/wind-speed", tags=["风速监测"])


@router.post("/report", summary="上报风速数据(每5秒一次)")
def api_report_wind_speed(report: WindSpeedReport):
    if report.crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {report.crane_id} 不存在")

    try:
        from wind_speed_monitor import is_crane_wind_shutdown, get_crane_wind_shutdown_info
        if is_crane_wind_shutdown(report.crane_id):
            shutdown_info = get_crane_wind_shutdown_info(report.crane_id)
            result = process_wind_speed_report(report)
            if result.get("recovery_triggered"):
                return {
                    "code": 0,
                    "message": "风速数据已记录，风速停机状态已自动恢复",
                    **result,
                }
            return {
                "code": 1,
                "message": "塔吊处于风速停机状态，风速数据已记录但状态上报被拒绝",
                "code_type": "WIND_SPEED_SHUTDOWN",
                "crane_id": report.crane_id,
                "wind_shutdown_at": shutdown_info.get("shutdown_at"),
                "wind_shutdown_reason": shutdown_info.get("shutdown_reason"),
                "consecutive_normal_count": result.get("consecutive_normal_count", 0),
                "auto_recovery_threshold": result.get("auto_recovery_threshold"),
                "recorded": result.get("recorded"),
                "wind_speed": result.get("wind_speed"),
            }
    except ImportError:
        pass

    result = process_wind_speed_report(report)

    if result.get("is_wind_shutdown"):
        return {
            "code": 2,
            "message": "风速超限，塔吊已进入风速停机状态",
            "code_type": "WIND_SPEED_SHUTDOWN_TRIGGERED",
            **result,
        }

    if result.get("alarm_triggered") and result.get("warning_alarm"):
        return {
            "code": 3,
            "message": "风速数据已记录，触发风速预警",
            "code_type": "WIND_SPEED_WARNING",
            **result,
        }

    return {
        "code": 0,
        "message": "风速数据已记录",
        **result,
    }


@router.get("/status/{crane_id}", summary="查询某台塔吊当前风速状态")
def api_get_wind_speed_status(crane_id: str):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")

    status = get_wind_speed_status(crane_id)
    if not status:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 风速状态不存在")

    return status


@router.get("/statuses", summary="查询所有塔吊风速状态")
def api_get_all_wind_speed_statuses():
    return get_all_wind_speed_statuses()


@router.get("/history/{crane_id}", summary="查询某台塔吊风速历史记录")
def api_get_wind_speed_history(
    crane_id: str,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    limit: int = 120,
):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")

    records = get_wind_speed_history(crane_id, start_time, end_time, limit)
    return {
        "crane_id": crane_id,
        "total": len(records),
        "records": records,
    }


@router.get("/alarms", summary="查询风速告警历史")
def api_get_wind_alarm_history(
    crane_id: Optional[str] = None,
    alarm_level: Optional[WindAlarmLevel] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    limit: int = 200,
):
    if crane_id and crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")

    alarms = get_wind_alarm_history(crane_id, alarm_level, start_time, end_time, limit)
    return {
        "total": len(alarms),
        "alarms": alarms,
    }


@router.get("/recoveries", summary="查询风速恢复事件历史")
def api_get_wind_recovery_history(
    crane_id: Optional[str] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    limit: int = 100,
):
    if crane_id and crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")

    recoveries = get_wind_recovery_history(crane_id, start_time, end_time, limit)
    return {
        "total": len(recoveries),
        "recoveries": recoveries,
    }


@router.post("/release/{crane_id}", summary="手动解除风速停机状态")
def api_manual_release_wind_shutdown(crane_id: str):
    result = manual_release_wind_shutdown(crane_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.put("/config", summary="修改风速阈值配置(热更新)")
def api_update_wind_config(request: WindSpeedThresholdUpdateRequest):
    try:
        result = update_wind_config(
            crane_id=request.crane_id,
            shutdown_threshold=request.shutdown_threshold,
            warning_threshold=request.warning_threshold,
            avg_window_seconds=request.avg_window_seconds,
            auto_recovery_consecutive_count=request.auto_recovery_consecutive_count,
            auto_recovery_threshold_ratio=request.auto_recovery_threshold_ratio,
            max_records_per_crane=request.max_records_per_crane,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/config/{crane_id}", summary="查询某台塔吊的风速配置")
def api_get_crane_wind_config(crane_id: str):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    return get_crane_wind_config(crane_id)


@router.get("/config", summary="查询全局默认风速配置")
def api_get_global_wind_config():
    return get_global_wind_config()


@router.get("/stats", summary="查询风速监测统计信息")
def api_get_wind_stats():
    return get_wind_stats()
