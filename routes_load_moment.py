from typing import List, Optional

from fastapi import APIRouter, HTTPException

from models import (
    WeightSensorReport,
    WeightRecord,
    OverloadAlarmEvent,
    OverloadAlarmLevel,
    RealtimeLoadStatus,
    LoadMomentEnvelopePoint,
    EnvelopeUpdateRequest,
)
from load_moment_monitor import (
    process_weight_report,
    get_weight_history,
    get_overload_alarm_history,
    get_realtime_load_status,
    get_all_realtime_load_statuses,
    get_envelope_curve,
    set_envelope_curve,
    get_overload_stats,
)
from collision import cranes_config

router = APIRouter(prefix="/api/load-moment", tags=["称重与超载监控"])


@router.post("/weight/report", summary="称重传感器上报实时载荷数据(每2秒一次)")
def report_weight_data(report: WeightSensorReport):
    if report.crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {report.crane_id} 不存在")
    if report.weight < 0:
        raise HTTPException(status_code=400, detail="重量值不能为负数")
    if report.sensor_timestamp <= 0:
        raise HTTPException(status_code=400, detail="传感器时间戳无效")
    try:
        result = process_weight_report(report)
        return {
            "code": 0,
            "message": "称重数据上报成功",
            "data": result,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/weight/history/{crane_id}", summary="查询某台塔吊的称重历史数据")
def query_weight_history(
    crane_id: str,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    limit: int = 300,
):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    if limit <= 0:
        limit = 300
    records = get_weight_history(crane_id, start_time, end_time, limit)
    return {
        "code": 0,
        "crane_id": crane_id,
        "total": len(records),
        "records": records,
    }


@router.get("/alarms", summary="查询超载告警历史(可按塔吊ID/告警级别筛选)")
def query_overload_alarms(
    crane_id: Optional[str] = None,
    alarm_level: Optional[OverloadAlarmLevel] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    limit: int = 200,
):
    if crane_id and crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    if limit <= 0:
        limit = 200
    alarms = get_overload_alarm_history(crane_id, alarm_level, start_time, end_time, limit)
    return {
        "code": 0,
        "total": len(alarms),
        "filters": {
            "crane_id": crane_id,
            "alarm_level": alarm_level.value if alarm_level else None,
            "start_time": start_time,
            "end_time": end_time,
        },
        "alarms": alarms,
    }


@router.get("/status/realtime/{crane_id}", summary="查询某台塔吊当前实时载荷状态")
def query_realtime_load_status(crane_id: str):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    status = get_realtime_load_status(crane_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 状态查询失败")
    return {
        "code": 0,
        "data": status,
    }


@router.get("/status/realtime", summary="查询所有塔吊当前实时载荷状态列表")
def query_all_realtime_load_statuses():
    statuses = get_all_realtime_load_statuses()
    return {
        "code": 0,
        "total": len(statuses),
        "statuses": statuses,
    }


@router.get("/envelope/{crane_id}", summary="查询某台塔吊的力矩包络曲线数据")
def query_envelope_curve(crane_id: str):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    envelope = get_envelope_curve(crane_id)
    if envelope is None:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 暂无力矩包络曲线配置")
    config = cranes_config[crane_id]
    return {
        "code": 0,
        "crane_id": crane_id,
        "crane_name": config.name,
        "arm_length": config.arm_length,
        "rated_max_load": config.max_load,
        "envelope_points": envelope,
    }


@router.put("/envelope", summary="更新某台塔吊的力矩包络曲线(热更新，不重启生效)")
def update_envelope_curve(request: EnvelopeUpdateRequest):
    if request.crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {request.crane_id} 不存在")
    try:
        success = set_envelope_curve(request.crane_id, request.envelope_points)
        if success:
            envelope = get_envelope_curve(request.crane_id)
            return {
                "code": 0,
                "message": f"塔吊 {request.crane_id} 力矩包络曲线更新成功",
                "crane_id": request.crane_id,
                "envelope_points": envelope,
            }
        else:
            raise HTTPException(status_code=500, detail="包络曲线更新失败")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/envelope/calculate-allowed", summary="根据变幅距离计算当前位置允许载荷")
def calculate_allowed_load_api(
    crane_id: str,
    trolley_position: float,
):
    from load_moment_monitor import calculate_allowed_load
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    if trolley_position < 0:
        raise HTTPException(status_code=400, detail="变幅小车位置不能为负数")
    allowed = calculate_allowed_load(crane_id, trolley_position)
    if allowed is None:
        raise HTTPException(status_code=404, detail="无法计算允许载荷，请检查包络曲线配置")
    return {
        "code": 0,
        "crane_id": crane_id,
        "trolley_position": trolley_position,
        "allowed_load_tons": round(allowed, 4),
    }


@router.get("/stats", summary="查询称重超载监控模块统计概览")
def query_load_moment_stats():
    stats = get_overload_stats()
    total_cranes = len(cranes_config)
    cranes_with_data = 0
    from load_moment_monitor import cranes_weight_records
    for crane_id in cranes_config:
        records = cranes_weight_records.get(crane_id)
        if records and len(records) > 0:
            cranes_with_data += 1
    return {
        "code": 0,
        "overview": {
            "total_cranes_registered": total_cranes,
            "cranes_with_weight_data": cranes_with_data,
            "cranes_without_data": total_cranes - cranes_with_data,
        },
        "alarm_stats": stats,
    }
