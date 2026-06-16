from typing import Optional

from fastapi import APIRouter, HTTPException

from models import (
    WorkOrderCreate,
    WorkOrderManualAssign,
    WorkOrderStatus,
)
from collision import cranes_config
from scheduler import (
    submit_order,
    manually_assign_order,
    cancel_order,
    reassign_order,
    start_order,
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


@router.post("/{order_id}/start", summary="标记工单开始执行")
def api_start_order(order_id: str):
    result = start_order(order_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "order": result["order"],
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
