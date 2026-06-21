from typing import Optional

from fastapi import APIRouter, HTTPException

from models import (
    WorkOrderCreate,
    WorkOrderManualAssign,
    WorkOrderStatus,
    PathPlanConfirmRequest,
)
from collision import cranes_config
from scheduler import (
    submit_order,
    manually_assign_order,
    cancel_order,
    reassign_order,
    start_order,
    confirm_and_start_order,
    complete_order,
    get_order,
    get_crane_queue,
    get_crane_history_records,
    get_all_orders,
)

router = APIRouter(prefix="/api/orders", tags=["工单调度"])


@router.post("", summary="提交吊装工单(自动分配)")
def api_submit_order(create: WorkOrderCreate):
    result = submit_order(create)
    if not result.get("assigned") and result.get("order"):
        return {
            "code": 1,
            "message": result["message"],
            "order": result["order"],
        }
    return {
        "code": 0,
        "message": result["message"],
        "order": result["order"],
    }


@router.get("/{order_id}", summary="查询工单状态")
def api_get_order(order_id: str):
    order = get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"工单 {order_id} 不存在")
    return order


@router.post("/{order_id}/assign", summary="手动指定塔吊(跳过自动分配)")
def api_manual_assign(order_id: str, req: WorkOrderManualAssign):
    result = manually_assign_order(order_id, req.crane_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "order": result["order"],
    }


@router.post("/{order_id}/cancel", summary="取消工单(仅待分配状态)")
def api_cancel_order(order_id: str):
    result = cancel_order(order_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "order": result["order"],
    }


@router.post("/{order_id}/reassign", summary="退回工单至待分配(重新调度)")
def api_reassign_order(order_id: str):
    result = reassign_order(order_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "order": result["order"],
    }


@router.post("/{order_id}/start", summary="标记工单开始执行(自动路径规划)")
def api_start_order(order_id: str):
    result = start_order(order_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    if result.get("code") == 2:
        return {
            "code": 2,
            "message": result["message"],
            "rehearsal": result["rehearsal"],
            "order": result["order"],
        }
    return {
        "code": 0,
        "message": result["message"],
        "order": result["order"],
        "path_plan": result.get("path_plan"),
        "token_results": result.get("token_results", []),
    }


@router.post("/{order_id}/confirm-path", summary="确认路径方案并开始执行")
def api_confirm_path(order_id: str, req: PathPlanConfirmRequest):
    result = confirm_and_start_order(order_id, req.direction)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "order": result["order"],
        "path_plan": result.get("path_plan"),
        "token_results": result.get("token_results", []),
    }


@router.post("/{order_id}/complete", summary="标记工单完成")
def api_complete_order(order_id: str):
    result = complete_order(order_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "order": result["order"],
        "token_release_results": result.get("token_release_results", []),
    }


@router.get("/crane/{crane_id}/queue", summary="查询塔吊工单队列")
def api_crane_queue(crane_id: str):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    queue = get_crane_queue(crane_id)
    return {
        "crane_id": crane_id,
        "queue_length": len(queue),
        "orders": queue,
    }


@router.get("/crane/{crane_id}/history", summary="查询塔吊历史执行记录")
def api_crane_history(crane_id: str):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    history = get_crane_history_records(crane_id)
    return {
        "crane_id": crane_id,
        "total_completed": len(history),
        "orders": history,
    }


@router.get("", summary="查询所有工单(可按状态过滤)")
def api_list_orders(status: Optional[WorkOrderStatus] = None):
    orders = get_all_orders(status)
    return {
        "total": len(orders),
        "orders": orders,
    }


@router.get("/{order_id}/progress", summary="查询工单实时进度")
def api_get_order_progress(order_id: str):
    from order_progress import get_order_progress
    progress = get_order_progress(order_id)
    if not progress:
        raise HTTPException(status_code=404, detail=f"工单 {order_id} 暂无进度数据")
    return progress


@router.get("/crane/{crane_id}/current-progress", summary="查询塔吊当前执行工单的进度")
def api_get_crane_current_progress(crane_id: str):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    from order_progress import get_crane_current_progress
    progress = get_crane_current_progress(crane_id)
    if not progress:
        return {
            "crane_id": crane_id,
            "has_executing_order": False,
            "message": "塔吊当前无正在执行的工单",
        }
    return {
        "crane_id": crane_id,
        "has_executing_order": True,
        "progress": progress,
    }


@router.get("/crane/{crane_id}/deviation-stats", summary="查询塔吊最近10单的平均耗时偏差比")
def api_get_crane_deviation_stats(crane_id: str):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    from order_progress import get_crane_deviation_ratio_stats
    return get_crane_deviation_ratio_stats(crane_id)


@router.get("/stagnation-alarms", summary="查询工单停滞告警历史")
def api_get_stagnation_alarms(crane_id: Optional[str] = None,
                              order_id: Optional[str] = None,
                              limit: int = 100):
    from order_progress import get_stagnation_alarms
    return get_stagnation_alarms(crane_id=crane_id, order_id=order_id, limit=limit)
