from typing import Optional

from fastapi import APIRouter, HTTPException

from models import (
    EmergencyLevel,
    EmergencyEventStatus,
    CompositeAlarmRuleCreate,
    CompositeAlarmRuleUpdate,
    EmergencyEventCloseRequest,
)
from emergency_response import (
    create_rule,
    update_rule,
    delete_rule,
    get_rule,
    list_rules,
    get_active_emergency_events,
    get_emergency_event,
    list_emergency_events,
    close_emergency_event,
    check_and_trigger_emergency,
)

router = APIRouter(prefix="/api/emergency", tags=["应急响应管理"])


@router.post("/rules", summary="创建复合告警规则")
def api_create_rule(req: CompositeAlarmRuleCreate):
    rule = create_rule(req)
    return {
        "code": 0,
        "message": "规则创建成功",
        "rule": rule,
    }


@router.get("/rules", summary="查询复合告警规则列表")
def api_list_rules(enabled_only: Optional[bool] = False):
    rules = list_rules(enabled_only=enabled_only)
    return {
        "code": 0,
        "total": len(rules),
        "rules": rules,
    }


@router.get("/rules/{rule_id}", summary="查询单条复合告警规则")
def api_get_rule(rule_id: str):
    rule = get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail=f"规则 {rule_id} 不存在")
    return {
        "code": 0,
        "rule": rule,
    }


@router.put("/rules/{rule_id}", summary="更新复合告警规则")
def api_update_rule(rule_id: str, req: CompositeAlarmRuleUpdate):
    rule = update_rule(rule_id, req)
    if not rule:
        raise HTTPException(status_code=404, detail=f"规则 {rule_id} 不存在")
    return {
        "code": 0,
        "message": "规则更新成功，立即生效",
        "rule": rule,
    }


@router.delete("/rules/{rule_id}", summary="删除复合告警规则")
def api_delete_rule(rule_id: str):
    ok = delete_rule(rule_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"规则 {rule_id} 不存在")
    return {
        "code": 0,
        "message": "规则删除成功",
    }


@router.post("/check", summary="手动触发复合告警检查")
def api_check_emergency():
    events = check_and_trigger_emergency()
    return {
        "code": 0,
        "message": f"复合告警检查完成，新触发 {len(events)} 个应急事件",
        "triggered_events": events,
    }


@router.get("/events/active", summary="查询当前活跃的应急事件列表")
def api_get_active_events():
    events = get_active_emergency_events()
    return {
        "code": 0,
        "total": len(events),
        "events": events,
    }


@router.get("/events/{event_id}", summary="查询应急事件详情(含关联告警快照)")
def api_get_event_detail(event_id: str):
    event = get_emergency_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail=f"应急事件 {event_id} 不存在")
    return {
        "code": 0,
        "event": event,
    }


@router.get("/events", summary="查询应急事件历史(按等级和时间范围筛选)")
def api_list_events(
    level: Optional[EmergencyLevel] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    status: Optional[EmergencyEventStatus] = None,
):
    events = list_emergency_events(
        level=level,
        start_time=start_time,
        end_time=end_time,
        status=status,
    )
    return {
        "code": 0,
        "total": len(events),
        "events": events,
    }


@router.post("/events/{event_id}/close", summary="关闭应急事件(需填写处置结果和关闭原因)")
def api_close_event(event_id: str, req: EmergencyEventCloseRequest):
    event = close_emergency_event(
        event_id=event_id,
        closed_by=req.closed_by,
        handling_result=req.handling_result,
        close_reason=req.close_reason,
    )
    if not event:
        raise HTTPException(status_code=404, detail=f"应急事件 {event_id} 不存在")
    return {
        "code": 0,
        "message": "应急事件已关闭",
        "event": event,
    }
