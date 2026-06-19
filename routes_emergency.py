from typing import Optional

from fastapi import APIRouter, HTTPException

from models import (
    EmergencyLevel,
    EmergencyEventStatus,
    CompositeAlarmRuleCreate,
    CompositeAlarmRuleUpdate,
    EmergencyEventCloseRequest,
    DrillInitiateRequest,
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
    initiate_drill,
    list_drill_reports,
    get_drill_report,
    get_rule_effectiveness,
    list_rules_effectiveness,
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
def api_get_active_events(include_drill: Optional[bool] = False):
    events = get_active_emergency_events(include_drill=include_drill)
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
    include_drill: Optional[bool] = False,
):
    events = list_emergency_events(
        level=level,
        start_time=start_time,
        end_time=end_time,
        status=status,
        include_drill=include_drill,
    )
    return {
        "code": 0,
        "total": len(events),
        "events": events,
    }


@router.post("/drill/initiate", summary="发起应急演练")
def api_initiate_drill(req: DrillInitiateRequest):
    try:
        report = initiate_drill(
            rule_id=req.rule_id,
            initiated_by=req.initiated_by,
            target_crane_ids=req.target_crane_ids,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "code": 0,
        "message": "演练完成",
        "drill_report": report,
    }


@router.get("/drill", summary="查询演练历史列表")
def api_list_drills(
    rule_id: Optional[str] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
):
    reports = list_drill_reports(
        rule_id=rule_id,
        start_time=start_time,
        end_time=end_time,
    )
    return {
        "code": 0,
        "total": len(reports),
        "drill_reports": reports,
    }


@router.get("/drill/{drill_id}", summary="查询单次演练详情(含模拟告警快照和动作报告)")
def api_get_drill_detail(drill_id: str):
    report = get_drill_report(drill_id)
    if not report:
        raise HTTPException(status_code=404, detail=f"演练 {drill_id} 不存在")
    return {
        "code": 0,
        "drill_report": report,
    }


@router.get("/effectiveness/rules", summary="查询所有规则的有效性评分")
def api_list_rules_effectiveness():
    scores = list_rules_effectiveness()
    return {
        "code": 0,
        "total": len(scores),
        "effectiveness_scores": scores,
    }


@router.get("/effectiveness/rules/{rule_id}", summary="查询单条规则的有效性评分和历史趋势")
def api_get_rule_effectiveness(rule_id: str):
    score = get_rule_effectiveness(rule_id)
    if not score:
        raise HTTPException(status_code=404, detail=f"规则 {rule_id} 不存在")
    return {
        "code": 0,
        "effectiveness_score": score,
    }


@router.post("/events/{event_id}/close", summary="关闭应急事件(需填写处置结果和关闭原因)")
def api_close_event(event_id: str, req: EmergencyEventCloseRequest):
    try:
        event = close_emergency_event(
            event_id=event_id,
            closed_by=req.closed_by,
            handling_result=req.handling_result,
            close_reason=req.close_reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not event:
        raise HTTPException(status_code=404, detail=f"应急事件 {event_id} 不存在")
    return {
        "code": 0,
        "message": "应急事件已关闭",
        "event": event,
    }
