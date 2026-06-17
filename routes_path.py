from typing import Optional

from fastapi import APIRouter, HTTPException

from models import PathDirection
from collision import cranes_config
from path_planner import (
    plan_path,
    rehearse_path_for_order,
    get_active_plans_for_crane,
    get_execution_history_for_crane,
)

router = APIRouter(prefix="/api/path", tags=["路径规划"])


class PathPlanRequest:
    pass


@router.post("/plan", summary="规划吊运回转路径")
def api_plan_path(
    crane_id: str,
    lift_x: float,
    lift_y: float,
    drop_x: float,
    drop_y: float,
    direction: PathDirection = PathDirection.CW,
):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    try:
        plan = plan_path(crane_id, lift_x, lift_y, drop_x, drop_y, direction)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "code": 0,
        "message": "路径规划完成",
        "plan": plan,
    }


@router.post("/rehearse/{order_id}", summary="路径冲突预演")
def api_rehearse_path(order_id: str):
    try:
        result = rehearse_path_for_order(order_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {
        "code": 0,
        "message": "预演完成",
        "rehearsal": result,
    }


@router.get("/active/{crane_id}", summary="查询塔吊当前活跃路径预案")
def api_active_plans(crane_id: str):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    plans = get_active_plans_for_crane(crane_id)
    return {
        "crane_id": crane_id,
        "active_plan_count": len(plans),
        "plans": plans,
    }


@router.get("/history/{crane_id}", summary="查询塔吊历史路径执行记录")
def api_execution_history(crane_id: str, limit: Optional[int] = 50):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    records = get_execution_history_for_crane(crane_id)
    return {
        "crane_id": crane_id,
        "total_records": len(records),
        "records": records[-limit:],
    }
