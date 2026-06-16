from typing import List, Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks

from models import (
    AlarmType,
    AnomalyDetectionConfig,
    SlidingWindowStats,
    AnomalyEvent,
    CraneFreezeStatus,
    CraneStatus,
)
from collision import cranes_config
from anomaly_detector import (
    anomaly_config,
    cranes_freeze_status,
    get_sliding_window_stats,
    get_anomaly_events,
    get_all_anomaly_events,
    process_status_report_async,
    is_crane_frozen,
    init_anomaly_detector,
)

router = APIRouter(prefix="/api/anomaly", tags=["异常检测"])


@router.post("/crane/status", summary="上报塔吊状态并异步触发异常检测(测试用)")
async def report_status_with_anomaly_detection(
    status: CraneStatus,
    background_tasks: BackgroundTasks
):
    if status.crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {status.crane_id} 不存在")

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

    background_tasks.add_task(process_status_report_async, status)

    return {
        "code": 0,
        "message": "状态上报已接收，异常检测将异步执行",
        "crane_id": status.crane_id,
        "anomaly_detection_triggered": True,
    }


@router.get("/config", summary="获取异常检测配置")
def get_anomaly_config():
    return anomaly_config


@router.put("/config", summary="更新异常检测配置")
def update_anomaly_config(config: AnomalyDetectionConfig):
    global anomaly_config
    anomaly_config = config
    init_anomaly_detector()
    return {
        "code": 0,
        "message": "配置已更新",
        "config": anomaly_config
    }


@router.get("/crane/{crane_id}/window-stats", summary="查询塔吊滑动窗口统计摘要")
def get_crane_window_stats(crane_id: str):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")

    stats = get_sliding_window_stats(crane_id)
    if not stats:
        return {
            "crane_id": crane_id,
            "message": "暂无足够数据，请先上报塔吊状态",
            "window_size": anomaly_config.sliding_window_size,
            "current_count": 0,
        }
    return stats


@router.get("/crane/{crane_id}/anomaly-events", summary="查询塔吊最近的异常事件列表")
def get_crane_anomaly_events(
    crane_id: str,
    alarm_type: Optional[AlarmType] = None,
    limit: int = 100
):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")

    events = get_anomaly_events(crane_id, limit)
    if alarm_type:
        events = [e for e in events if e.alarm_type == alarm_type]
    return {
        "crane_id": crane_id,
        "total_events": len(events),
        "events": events
    }


@router.get("/anomaly-events", summary="查询所有塔吊的异常事件列表")
def list_all_anomaly_events(
    alarm_type: Optional[AlarmType] = None,
    limit: int = 100
):
    events = get_all_anomaly_events(limit)
    if alarm_type:
        events = [e for e in events if e.alarm_type == alarm_type]
    return {
        "total_events": len(events),
        "events": events
    }


@router.get("/crane/{crane_id}/freeze-status", summary="查询塔吊冻结状态")
def get_crane_freeze_status(crane_id: str):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")

    if crane_id not in cranes_freeze_status:
        return CraneFreezeStatus(crane_id=crane_id, is_frozen=False)

    is_crane_frozen(crane_id)
    return cranes_freeze_status[crane_id]


@router.get("/freeze-statuses", summary="查询所有塔吊冻结状态")
def get_all_freeze_statuses():
    result = []
    for crane_id in cranes_config.keys():
        is_crane_frozen(crane_id)
        status = cranes_freeze_status.get(
            crane_id,
            CraneFreezeStatus(crane_id=crane_id, is_frozen=False)
        )
        result.append(status)
    return result


@router.get("/all-window-stats", summary="查询所有塔吊的滑动窗口统计摘要")
def get_all_window_stats():
    result = []
    for crane_id in cranes_config.keys():
        stats = get_sliding_window_stats(crane_id)
        if stats:
            result.append(stats)
        else:
            result.append({
                "crane_id": crane_id,
                "message": "暂无足够数据",
                "window_size": anomaly_config.sliding_window_size,
                "current_count": 0,
            })
    return result
