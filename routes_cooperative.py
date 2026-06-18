from typing import Optional

from fastapi import APIRouter, HTTPException

from models import (
    CooperativeLiftCreate,
    CooperativeLiftReadyConfirm,
    CooperativeLiftSyncAck,
    CooperativeLiftCompleteConfirm,
    CooperativeLiftStatus,
)
from collision import cranes_config
from cooperative_lift import (
    create_cooperative_task,
    confirm_ready,
    ack_sync_command,
    confirm_complete,
    get_task,
    get_all_tasks,
    get_crane_tasks,
    abort_task,
)

router = APIRouter(prefix="/api/cooperative-lift", tags=["协同吊装"])


@router.post("", summary="创建协同吊装任务")
def api_create_cooperative_task(create: CooperativeLiftCreate):
    result = create_cooperative_task(create)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result)
    return {
        "code": 0,
        "message": result["message"],
        "task": result["task"],
    }


@router.post("/{task_id}/confirm-ready", summary="操作员确认就绪")
def api_confirm_ready(task_id: str, req: CooperativeLiftReadyConfirm):
    result = confirm_ready(task_id, req.crane_id, req.operator_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "task": result["task"],
        **{k: v for k, v in result.items() if k not in ("message", "task")},
    }


@router.post("/{task_id}/ack-sync", summary="确认收到同步指令")
def api_ack_sync(task_id: str, req: CooperativeLiftSyncAck):
    result = ack_sync_command(task_id, req.crane_id, req.operator_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "task": result["task"],
        **{k: v for k, v in result.items() if k not in ("message", "task")},
    }


@router.post("/{task_id}/complete", summary="发起人确认完成")
def api_confirm_complete(task_id: str, req: CooperativeLiftCompleteConfirm):
    result = confirm_complete(task_id, req.initiator)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {
        "code": 0,
        "message": result["message"],
        "task": result["task"],
    }


@router.post("/{task_id}/abort", summary="强制中止协同吊装任务")
def api_abort_task(task_id: str, reason: str = "人工强制中止"):
    task = abort_task(task_id, reason)
    if not task:
        raise HTTPException(status_code=404, detail=f"协同吊装任务 {task_id} 不存在")
    return {
        "code": 0,
        "message": f"协同吊装任务 {task_id} 已中止",
        "task": task,
    }


@router.get("/{task_id}", summary="查询协同吊装任务详情")
def api_get_task(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"协同吊装任务 {task_id} 不存在")
    return task


@router.get("", summary="查询所有协同吊装任务")
def api_list_tasks(status: Optional[CooperativeLiftStatus] = None):
    tasks = get_all_tasks(status)
    return {
        "total": len(tasks),
        "tasks": tasks,
    }


@router.get("/crane/{crane_id}/history", summary="查询某台塔吊参与的所有协同任务记录")
def api_get_crane_tasks(crane_id: str):
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    tasks = get_crane_tasks(crane_id)
    return {
        "crane_id": crane_id,
        "total": len(tasks),
        "tasks": tasks,
    }
