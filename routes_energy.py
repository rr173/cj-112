from typing import Optional

from fastapi import APIRouter, HTTPException

from models import (
    EnergyAlarmLevel,
    EnergyMeterReport,
    EnergyQuotaUpdateRequest,
)
from energy_monitor import (
    process_energy_meter_report,
    get_energy_status,
    get_all_energy_statuses,
    get_energy_history,
    get_energy_alarm_history,
    update_energy_quota,
    get_energy_ranking,
    get_energy_stats,
)
from collision import cranes_config

router = APIRouter(prefix="/api/energy", tags=["能耗监测"])


@router.post("/report", summary="上报电表数据(每10秒一次)")
def api_report_energy(report: EnergyMeterReport):
    if report.crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {report.crane_id} 不存在")

    try:
        result = process_energy_meter_report(report)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if result.get("alarm_triggered") and result.get("latest_alarm"):
        alarm = result["latest_alarm"]
        if alarm.alarm_level == EnergyAlarmLevel.RED:
            return {
                "code": 2,
                "code_type": "ENERGY_QUOTA_EXCEEDED",
                "message": "能耗数据已记录，触发能耗超限告警(红色)，塔吊已标记为能耗超限状态",
                **result,
            }
        elif alarm.alarm_level == EnergyAlarmLevel.YELLOW:
            return {
                "code": 3,
                "code_type": "ENERGY_QUOTA_WARNING",
                "message": "能耗数据已记录，触发能耗预警(黄色)",
                **result,
            }

    return {
        "code": 0,
        "message": "能耗数据已记录",
        **result,
    }


@router.get("/status/{crane_id}", summary="查询某台塔吊实时能耗状态")
def api_get_energy_status(crane_id: str):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")

    status = get_energy_status(crane_id)
    if not status:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 能耗状态不存在")

    return status


@router.get("/statuses", summary="查询所有塔吊能耗状态")
def api_get_all_energy_statuses():
    return get_all_energy_statuses()


@router.get("/history/{crane_id}", summary="查询某台塔吊能耗历史(按时间范围)")
def api_get_energy_history(
    crane_id: str,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    limit: int = 500,
):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")

    records = get_energy_history(crane_id, start_time, end_time, limit)
    return {
        "crane_id": crane_id,
        "total": len(records),
        "records": records,
    }


@router.get("/alarms", summary="查询能耗告警历史(按级别筛选)")
def api_get_energy_alarm_history(
    crane_id: Optional[str] = None,
    alarm_level: Optional[EnergyAlarmLevel] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    limit: int = 200,
):
    if crane_id and crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")

    alarms = get_energy_alarm_history(crane_id, alarm_level, start_time, end_time, limit)
    return {
        "total": len(alarms),
        "alarms": alarms,
    }


@router.put("/quota", summary="修改每台塔吊的日能耗配额(热更新)")
def api_update_energy_quota(request: EnergyQuotaUpdateRequest):
    try:
        result = update_energy_quota(
            crane_id=request.crane_id,
            daily_quota_kwh=request.daily_quota_kwh,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/ranking", summary="查询所有塔吊当日能耗排行(按累计降序)")
def api_get_energy_ranking():
    ranking = get_energy_ranking()
    return {
        "total": len(ranking),
        "ranking": ranking,
    }


@router.get("/stats", summary="查询能耗监测统计信息")
def api_get_energy_stats():
    return get_energy_stats()
