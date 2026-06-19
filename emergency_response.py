import time
import uuid
import math
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from models import (
    AlarmType,
    EmergencyLevel,
    RuleScope,
    CompositeAlarmRule,
    CompositeAlarmRuleCreate,
    CompositeAlarmRuleUpdate,
    CompositeAlarmRuleCondition,
    EmergencyActionType,
    EmergencyActionStatus,
    EmergencyActionExecution,
    EmergencyEventStatus,
    GenericAlarmSnapshot,
    EmergencyEvent,
    EmergencyDailyStats,
    DrillReport,
    DrillActionReport,
    RuleEffectivenessRecord,
    RuleEffectivenessScore,
    EscalationLog,
)
from collision import cranes_config, alarm_history, lock_crane as collision_lock_crane


composite_alarm_rules: Dict[str, CompositeAlarmRule] = {}
emergency_events: Dict[str, EmergencyEvent] = {}
_active_rule_crane_dedup: Dict[Tuple[str, str], str] = {}
drill_reports: Dict[str, DrillReport] = {}
rule_effectiveness_records: Dict[str, List[RuleEffectivenessRecord]] = {}
EFFECTIVENESS_HISTORY_WINDOW = 10
MAINTENANCE_CHECK_WINDOW = 3
ESCALATION_TIMEOUTS: Dict[EmergencyLevel, float] = {
    EmergencyLevel.GENERAL: 1800.0,
    EmergencyLevel.SERIOUS: 900.0,
}
ESCALATION_LEVEL_ORDER = [EmergencyLevel.GENERAL, EmergencyLevel.SERIOUS, EmergencyLevel.CRITICAL]


def _datetime_str(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _compute_crane_distance(a_id: str, b_id: str) -> float:
    a = cranes_config.get(a_id)
    b = cranes_config.get(b_id)
    if not a or not b:
        return float("inf")
    dx = a.tower_x - b.tower_x
    dy = a.tower_y - b.tower_y
    return math.sqrt(dx * dx + dy * dy)


def are_cranes_adjacent(a_id: str, b_id: str, max_distance: float = 100.0) -> bool:
    if a_id == b_id:
        return False
    dist = _compute_crane_distance(a_id, b_id)
    return dist <= max_distance


def _collect_all_alarms() -> List[GenericAlarmSnapshot]:
    snapshots: List[GenericAlarmSnapshot] = []

    for alarm in alarm_history:
        crane_ids = []
        if alarm.crane_a_id:
            crane_ids.append(alarm.crane_a_id)
        if alarm.crane_b_id and alarm.crane_b_id not in crane_ids:
            crane_ids.append(alarm.crane_b_id)
        snapshots.append(GenericAlarmSnapshot(
            alarm_id=alarm.alarm_id,
            alarm_type=alarm.alarm_type,
            timestamp=alarm.timestamp,
            datetime_str=alarm.datetime_str,
            crane_ids=crane_ids,
            message=alarm.message,
            details=alarm.details if hasattr(alarm, 'details') else {},
        ))

    try:
        from wind_speed_monitor import cranes_wind_alarms
        for alarm in cranes_wind_alarms:
            snapshots.append(GenericAlarmSnapshot(
                alarm_id=alarm.alarm_id,
                alarm_type=alarm.alarm_type,
                timestamp=alarm.timestamp,
                datetime_str=alarm.datetime_str,
                crane_ids=[alarm.crane_id],
                message=alarm.message,
                details=alarm.details if hasattr(alarm, 'details') else {},
            ))
    except ImportError:
        pass

    try:
        from load_moment_monitor import cranes_overload_alarms
        for alarm in cranes_overload_alarms:
            snapshots.append(GenericAlarmSnapshot(
                alarm_id=alarm.alarm_id,
                alarm_type=AlarmType.LOAD_MOMENT_WARNING,
                timestamp=alarm.timestamp,
                datetime_str=alarm.datetime_str,
                crane_ids=[alarm.crane_id],
                message=alarm.message,
                details={
                    "alarm_level": alarm.alarm_level.value if hasattr(alarm.alarm_level, 'value') else str(alarm.alarm_level),
                    "overload_ratio": alarm.overload_ratio,
                },
            ))
    except ImportError:
        pass

    try:
        from energy_monitor import cranes_energy_alarms
        for alarm in cranes_energy_alarms:
            snapshots.append(GenericAlarmSnapshot(
                alarm_id=alarm.alarm_id,
                alarm_type=alarm.alarm_type,
                timestamp=alarm.timestamp,
                datetime_str=alarm.datetime_str,
                crane_ids=[alarm.crane_id],
                message=alarm.message,
                details=alarm.details if hasattr(alarm, 'details') else {},
            ))
    except ImportError:
        pass

    try:
        from anomaly_detector import cranes_anomaly_events
        for crane_id, events in cranes_anomaly_events.items():
            for event in events:
                snapshots.append(GenericAlarmSnapshot(
                    alarm_id=event.event_id,
                    alarm_type=event.alarm_type,
                    timestamp=event.timestamp,
                    datetime_str=event.datetime_str,
                    crane_ids=[crane_id],
                    message=event.message,
                    details=event.details if hasattr(event, 'details') else {},
                ))
    except ImportError:
        pass

    try:
        from maintenance import maintenance_alarms
        for alarm in maintenance_alarms:
            snapshots.append(GenericAlarmSnapshot(
                alarm_id=alarm.alarm_id,
                alarm_type=AlarmType.LOAD_MOMENT_WARNING,
                timestamp=alarm.timestamp,
                datetime_str=alarm.datetime_str,
                crane_ids=[alarm.crane_id],
                message=alarm.message,
                details=alarm.details if hasattr(alarm, 'details') else {},
            ))
    except ImportError:
        pass

    snapshots.sort(key=lambda x: x.timestamp)
    return snapshots


def init_emergency_response_module():
    now = time.time()
    now_str = _datetime_str(now)

    preset_rules = [
        CompositeAlarmRule(
            rule_id=str(uuid.uuid4()),
            name="单台塔吊力矩超限+超载",
            description="同一台塔吊在60秒内同时触发力矩超限和超载告警升级为严重",
            scope=RuleScope.SINGLE_CRANE,
            conditions=[
                CompositeAlarmRuleCondition(
                    alarm_types=[AlarmType.LOAD_MOMENT_WARNING],
                    min_count=1,
                    time_window_seconds=60,
                ),
                CompositeAlarmRuleCondition(
                    alarm_types=[AlarmType.ENERGY_QUOTA_EXCEEDED],
                    min_count=1,
                    time_window_seconds=60,
                ),
            ],
            emergency_level=EmergencyLevel.SERIOUS,
            enabled=True,
            created_at=now,
            updated_at=now,
        ),
        CompositeAlarmRule(
            rule_id=str(uuid.uuid4()),
            name="相邻塔吊碰撞预警",
            description="任意两台相邻塔吊在120秒内分别触发碰撞预警升级为紧急",
            scope=RuleScope.ADJACENT_CRANES,
            conditions=[
                CompositeAlarmRuleCondition(
                    alarm_types=[AlarmType.COLLISION],
                    min_count=2,
                    time_window_seconds=120,
                ),
            ],
            emergency_level=EmergencyLevel.CRITICAL,
            enabled=True,
            created_at=now,
            updated_at=now,
        ),
        CompositeAlarmRule(
            rule_id=str(uuid.uuid4()),
            name="单台塔吊告警风暴",
            description="单台塔吊在300秒内累计触发3条及以上任意类型告警升级为严重",
            scope=RuleScope.SINGLE_CRANE,
            conditions=[
                CompositeAlarmRuleCondition(
                    alarm_types=list(AlarmType),
                    min_count=3,
                    time_window_seconds=300,
                ),
            ],
            emergency_level=EmergencyLevel.SERIOUS,
            enabled=True,
            created_at=now,
            updated_at=now,
        ),
        CompositeAlarmRule(
            rule_id=str(uuid.uuid4()),
            name="风速停机+碰撞风险",
            description="任意塔吊在180秒内同时出现风速停机和碰撞预警升级为紧急",
            scope=RuleScope.ANY_CRANES,
            conditions=[
                CompositeAlarmRuleCondition(
                    alarm_types=[AlarmType.WIND_SPEED_SHUTDOWN, AlarmType.COLLISION],
                    min_count=2,
                    time_window_seconds=180,
                ),
            ],
            emergency_level=EmergencyLevel.CRITICAL,
            enabled=True,
            created_at=now,
            updated_at=now,
        ),
        CompositeAlarmRule(
            rule_id=str(uuid.uuid4()),
            name="能耗超限+力矩预警",
            description="同一台塔吊在300秒内同时出现能耗超标和力矩预警升级为一般",
            scope=RuleScope.SINGLE_CRANE,
            conditions=[
                CompositeAlarmRuleCondition(
                    alarm_types=[AlarmType.ENERGY_QUOTA_EXCEEDED, AlarmType.LOAD_MOMENT_WARNING],
                    min_count=2,
                    time_window_seconds=300,
                ),
            ],
            emergency_level=EmergencyLevel.GENERAL,
            enabled=True,
            created_at=now,
            updated_at=now,
        ),
    ]

    for rule in preset_rules:
        composite_alarm_rules[rule.rule_id] = rule

    print(f"[应急响应模块] 初始化完成，已加载 {len(composite_alarm_rules)} 条预置复合告警规则")


def create_rule(req: CompositeAlarmRuleCreate) -> CompositeAlarmRule:
    now = time.time()
    rule = CompositeAlarmRule(
        rule_id=str(uuid.uuid4()),
        name=req.name,
        description=req.description,
        scope=req.scope,
        conditions=req.conditions,
        emergency_level=req.emergency_level,
        enabled=req.enabled,
        created_at=now,
        updated_at=now,
    )
    composite_alarm_rules[rule.rule_id] = rule
    return rule


def update_rule(rule_id: str, req: CompositeAlarmRuleUpdate) -> Optional[CompositeAlarmRule]:
    rule = composite_alarm_rules.get(rule_id)
    if not rule:
        return None
    now = time.time()
    if req.name is not None:
        rule.name = req.name
    if req.description is not None:
        rule.description = req.description
    if req.scope is not None:
        rule.scope = req.scope
    if req.conditions is not None:
        rule.conditions = req.conditions
    if req.emergency_level is not None:
        rule.emergency_level = req.emergency_level
    if req.enabled is not None:
        rule.enabled = req.enabled
    rule.updated_at = now
    return rule


def delete_rule(rule_id: str) -> bool:
    if rule_id in composite_alarm_rules:
        del composite_alarm_rules[rule_id]
        return True
    return False


def get_rule(rule_id: str) -> Optional[CompositeAlarmRule]:
    return composite_alarm_rules.get(rule_id)


def list_rules(enabled_only: bool = False) -> List[CompositeAlarmRule]:
    rules = list(composite_alarm_rules.values())
    if enabled_only:
        rules = [r for r in rules if r.enabled]
    rules.sort(key=lambda r: r.created_at, reverse=True)
    return rules


def _check_condition_match(
    condition: CompositeAlarmRuleCondition,
    alarms: List[GenericAlarmSnapshot],
    scope: RuleScope,
    crane_group: Optional[List[str]] = None,
) -> Tuple[bool, List[GenericAlarmSnapshot]]:
    now = time.time()
    window_start = now - condition.time_window_seconds

    relevant_alarms = [
        a for a in alarms
        if a.timestamp >= window_start and a.alarm_type in condition.alarm_types
    ]

    if scope == RuleScope.SINGLE_CRANE and crane_group:
        target_crane = crane_group[0]
        relevant_alarms = [a for a in relevant_alarms if target_crane in a.crane_ids]
    elif scope == RuleScope.ADJACENT_CRANES and crane_group:
        relevant_alarms = [
            a for a in relevant_alarms
            if any(cid in crane_group for cid in a.crane_ids)
        ]
    elif scope == RuleScope.ANY_CRANES and crane_group:
        relevant_alarms = [
            a for a in relevant_alarms
            if any(cid in crane_group for cid in a.crane_ids)
        ]

    matched = len(relevant_alarms) >= condition.min_count

    if matched and len(condition.alarm_types) > 1:
        type_counts: Dict[AlarmType, int] = {}
        for a in relevant_alarms:
            type_counts[a.alarm_type] = type_counts.get(a.alarm_type, 0) + 1
        for atype in condition.alarm_types:
            if type_counts.get(atype, 0) < 1:
                matched = False
                break

    return matched, relevant_alarms


def _check_rule_match(
    rule: CompositeAlarmRule,
    all_alarms: List[GenericAlarmSnapshot],
) -> Tuple[bool, List[str], List[GenericAlarmSnapshot]]:
    if not rule.enabled:
        return False, [], []

    crane_ids = list(cranes_config.keys())

    if rule.scope == RuleScope.SINGLE_CRANE:
        for crane_id in crane_ids:
            all_conditions_met = True
            matched_alarms: List[GenericAlarmSnapshot] = []
            for condition in rule.conditions:
                cond_met, cond_alarms = _check_condition_match(
                    condition, all_alarms, rule.scope, [crane_id]
                )
                if not cond_met:
                    all_conditions_met = False
                    break
                matched_alarms.extend(cond_alarms)
            if all_conditions_met and matched_alarms:
                dedup_alarms = list({a.alarm_id: a for a in matched_alarms}.values())
                return True, [crane_id], dedup_alarms

    elif rule.scope == RuleScope.ADJACENT_CRANES:
        for i in range(len(crane_ids)):
            for j in range(i + 1, len(crane_ids)):
                if not are_cranes_adjacent(crane_ids[i], crane_ids[j]):
                    continue
                pair = [crane_ids[i], crane_ids[j]]
                all_conditions_met = True
                matched_alarms: List[GenericAlarmSnapshot] = []
                for condition in rule.conditions:
                    cond_met, cond_alarms = _check_condition_match(
                        condition, all_alarms, rule.scope, pair
                    )
                    if not cond_met:
                        all_conditions_met = False
                        break
                    matched_alarms.extend(cond_alarms)
                if all_conditions_met and matched_alarms:
                    has_both = any(crane_ids[i] in a.crane_ids for a in matched_alarms) and \
                               any(crane_ids[j] in a.crane_ids for a in matched_alarms)
                    if has_both or len(matched_alarms) >= 2:
                        dedup_alarms = list({a.alarm_id: a for a in matched_alarms}.values())
                        return True, pair, dedup_alarms

    elif rule.scope == RuleScope.ANY_CRANES:
        all_conditions_met = True
        matched_alarms: List[GenericAlarmSnapshot] = []
        for condition in rule.conditions:
            cond_met, cond_alarms = _check_condition_match(
                condition, all_alarms, rule.scope, crane_ids
            )
            if not cond_met:
                all_conditions_met = False
                break
            matched_alarms.extend(cond_alarms)
        if all_conditions_met and matched_alarms:
            affected_cranes: Set[str] = set()
            for a in matched_alarms:
                affected_cranes.update(a.crane_ids)
            dedup_alarms = list({a.alarm_id: a for a in matched_alarms}.values())
            return True, list(affected_cranes), dedup_alarms

    return False, [], []


def _build_emergency_plan(level: EmergencyLevel, crane_ids: List[str]) -> List[EmergencyActionExecution]:
    actions: List[EmergencyActionExecution] = []

    if level in [EmergencyLevel.SERIOUS, EmergencyLevel.CRITICAL]:
        actions.append(EmergencyActionExecution(
            action_type=EmergencyActionType.LOCK_CRANE,
            target_crane_ids=list(crane_ids),
        ))

    actions.append(EmergencyActionExecution(
        action_type=EmergencyActionType.NOTIFY_SAFETY_OFFICER,
        target_crane_ids=list(crane_ids),
    ))

    if level in [EmergencyLevel.SERIOUS, EmergencyLevel.CRITICAL]:
        actions.append(EmergencyActionExecution(
            action_type=EmergencyActionType.NOTIFY_PROJECT_MANAGER,
            target_crane_ids=list(crane_ids),
        ))

    actions.append(EmergencyActionExecution(
        action_type=EmergencyActionType.NOTIFY_OPERATOR,
        target_crane_ids=list(crane_ids),
    ))

    if level in [EmergencyLevel.SERIOUS, EmergencyLevel.CRITICAL]:
        actions.append(EmergencyActionExecution(
            action_type=EmergencyActionType.SUSPEND_WORK_ORDERS,
            target_crane_ids=list(crane_ids),
        ))

    if level == EmergencyLevel.CRITICAL:
        actions.append(EmergencyActionExecution(
            action_type=EmergencyActionType.MARK_AFFECTED_WORK_ORDERS,
            target_crane_ids=list(crane_ids),
        ))
        actions.append(EmergencyActionExecution(
            action_type=EmergencyActionType.NOTIFY_SITE_COORDINATION,
            target_crane_ids=list(crane_ids),
        ))
        actions.append(EmergencyActionExecution(
            action_type=EmergencyActionType.TRIGGER_BROADCAST,
            target_crane_ids=list(crane_ids),
        ))

    return actions


def _execute_action(action: EmergencyActionExecution, event: EmergencyEvent) -> None:
    start_time = time.time()
    try:
        if action.action_type == EmergencyActionType.LOCK_CRANE:
            if event.is_drill:
                action.status = EmergencyActionStatus.SUCCESS
                action.result_message = f"[演练] 模拟锁定 {len(action.target_crane_ids)} 台塔吊: {', '.join(action.target_crane_ids)} (未真实锁定)"
                action.details = {
                    "drill_mode": True,
                    "simulated_cranes": action.target_crane_ids,
                    "lock_reason": f"演练事件: {event.event_id}, 规则: {event.rule_name}",
                }
            else:
                for crane_id in action.target_crane_ids:
                    try:
                        collision_lock_crane(crane_id, f"应急事件锁定: {event.event_id}, 规则: {event.rule_name}")
                    except Exception:
                        pass
                action.status = EmergencyActionStatus.SUCCESS
                action.result_message = f"已锁定 {len(action.target_crane_ids)} 台塔吊: {', '.join(action.target_crane_ids)}"

        elif action.action_type in [
            EmergencyActionType.NOTIFY_SAFETY_OFFICER,
            EmergencyActionType.NOTIFY_PROJECT_MANAGER,
            EmergencyActionType.NOTIFY_OPERATOR,
        ]:
            role_map = {
                EmergencyActionType.NOTIFY_SAFETY_OFFICER: "安全员",
                EmergencyActionType.NOTIFY_PROJECT_MANAGER: "项目经理",
                EmergencyActionType.NOTIFY_OPERATOR: "操作员",
            }
            role = role_map.get(action.action_type, "相关人员")
            if event.is_drill:
                action.status = EmergencyActionStatus.SUCCESS
                action.result_message = f"[演练] 模拟通知{role}: 应急事件 {event.event_id}, 等级 {event.emergency_level.value} (未真实发送)"
                action.details = {
                    "drill_mode": True,
                    "event_id": event.event_id,
                    "emergency_level": event.emergency_level.value,
                    "affected_cranes": action.target_crane_ids,
                    "notified_at": start_time,
                    "simulated": True,
                }
            else:
                action.status = EmergencyActionStatus.SUCCESS
                action.result_message = f"已通知{role}: 应急事件 {event.event_id}, 等级 {event.emergency_level.value}"
                action.details = {
                    "event_id": event.event_id,
                    "emergency_level": event.emergency_level.value,
                    "affected_cranes": action.target_crane_ids,
                    "notified_at": start_time,
                }

        elif action.action_type == EmergencyActionType.SUSPEND_WORK_ORDERS:
            try:
                from scheduler import work_orders, WorkOrderStatus
                if event.is_drill:
                    potential_count = 0
                    potential_order_ids: List[str] = []
                    for order in work_orders.values():
                        if order.assigned_crane_id in action.target_crane_ids and \
                           order.status in [WorkOrderStatus.PENDING, WorkOrderStatus.ASSIGNED]:
                            potential_count += 1
                            potential_order_ids.append(order.order_id)
                    action.status = EmergencyActionStatus.SUCCESS
                    action.result_message = f"[演练] 模拟暂停 {potential_count} 个相关工单 (未真实暂停)"
                    action.details = {
                        "drill_mode": True,
                        "simulated": True,
                        "would_suspend_count": potential_count,
                        "would_suspend_order_ids": potential_order_ids,
                    }
                else:
                    suspended_count = 0
                    suspended_order_ids: List[str] = []
                    for order in work_orders.values():
                        if order.assigned_crane_id in action.target_crane_ids and \
                           order.status in [WorkOrderStatus.PENDING, WorkOrderStatus.ASSIGNED]:
                            order.previous_status = order.status
                            order.status = WorkOrderStatus.SUSPENDED
                            order.suspended_by_emergency_event_id = event.event_id
                            order.failure_reason = f"应急事件暂停: {event.event_id}"
                            suspended_count += 1
                            suspended_order_ids.append(order.order_id)
                    action.status = EmergencyActionStatus.SUCCESS
                    action.result_message = f"已暂停 {suspended_count} 个相关工单(应急事件关闭后可恢复)"
                    action.details = {
                        "suspended_count": suspended_count,
                        "suspended_order_ids": suspended_order_ids,
                    }
            except ImportError:
                action.status = EmergencyActionStatus.SKIPPED
                action.result_message = "工单模块不可用，跳过工单暂停"

        elif action.action_type == EmergencyActionType.TRIGGER_BROADCAST:
            if event.is_drill:
                action.status = EmergencyActionStatus.SUCCESS
                action.result_message = f"[演练] 模拟触发全场广播: 应急事件 {event.event_id}, 等级 {event.emergency_level.value} (未真实广播)"
                action.details = {
                    "drill_mode": True,
                    "simulated": True,
                    "event_id": event.event_id,
                    "emergency_level": event.emergency_level.value,
                    "broadcast_content": f"[演练] 紧急广播：触发{event.emergency_level.value}级应急事件，请相关人员注意！涉及塔吊: {', '.join(action.target_crane_ids)}",
                    "broadcast_at": start_time,
                }
            else:
                action.status = EmergencyActionStatus.SUCCESS
                action.result_message = f"全场广播已触发: 应急事件 {event.event_id}, 等级 {event.emergency_level.value}"
                action.details = {
                    "event_id": event.event_id,
                    "emergency_level": event.emergency_level.value,
                    "broadcast_content": f"紧急广播：触发{event.emergency_level.value}级应急事件，请相关人员立即处置！涉及塔吊: {', '.join(action.target_crane_ids)}",
                    "broadcast_at": start_time,
                }

        elif action.action_type == EmergencyActionType.MARK_AFFECTED_WORK_ORDERS:
            try:
                from scheduler import work_orders, WorkOrderStatus
                if event.is_drill:
                    potential_count = sum(1 for o in work_orders.values()
                                         if o.status == WorkOrderStatus.EXECUTING)
                    action.status = EmergencyActionStatus.SUCCESS
                    action.result_message = f"[演练] 模拟标记 {potential_count} 个执行中工单受影响 (未真实标记)"
                    action.details = {
                        "drill_mode": True,
                        "simulated": True,
                        "would_mark_count": potential_count,
                    }
                else:
                    marked_count = 0
                    marked_order_ids: List[str] = []
                    for order in work_orders.values():
                        if order.status == WorkOrderStatus.EXECUTING:
                            if event.event_id not in order.affected_by_emergency_event_ids:
                                order.affected_by_emergency_event_ids.append(event.event_id)
                                marked_count += 1
                                marked_order_ids.append(order.order_id)
                    action.status = EmergencyActionStatus.SUCCESS
                    action.result_message = f"已标记 {marked_count} 个执行中工单受紧急事件影响"
                    action.details = {
                        "marked_count": marked_count,
                        "marked_order_ids": marked_order_ids,
                    }
            except ImportError:
                action.status = EmergencyActionStatus.SKIPPED
                action.result_message = "工单模块不可用，跳过受影响工单标记"

        elif action.action_type == EmergencyActionType.NOTIFY_SITE_COORDINATION:
            if event.is_drill:
                action.status = EmergencyActionStatus.SUCCESS
                action.result_message = f"[演练] 模拟发送全场协调通知 (未真实发送)"
                action.details = {
                    "drill_mode": True,
                    "simulated": True,
                    "event_id": event.event_id,
                    "emergency_level": event.emergency_level.value,
                }
            else:
                action.status = EmergencyActionStatus.SUCCESS
                action.result_message = f"全场协调通知已发送: 紧急事件 {event.event_id}"
                action.details = {
                    "event_id": event.event_id,
                    "emergency_level": event.emergency_level.value,
                    "notification_type": "SITE_COORDINATION",
                    "content": f"全场协调通知: 工地发生紧急事件({event.event_id}), 涉及塔吊: {', '.join(action.target_crane_ids)}, 请相关人员注意协调",
                    "sent_at": start_time,
                }

    except Exception as e:
        action.status = EmergencyActionStatus.FAILED
        action.result_message = f"执行失败: {str(e)}"

    now = time.time()
    action.executed_at = start_time
    action.execution_duration_ms = round((now - start_time) * 1000, 2)


def _get_dedup_key(rule_id: str, crane_ids: List[str]) -> Tuple[str, str]:
    return (rule_id, "|".join(sorted(set(crane_ids))))


def _get_real_active_dedup_keys() -> Set[Tuple[str, str]]:
    active_events = [e for e in emergency_events.values() 
                     if e.status != EmergencyEventStatus.CLOSED and not e.is_drill]
    active_dedup_keys: Set[Tuple[str, str]] = set()
    for event in active_events:
        active_dedup_keys.add(_get_dedup_key(event.rule_id, event.affected_crane_ids))
    return active_dedup_keys


def _compute_plan_execution_delay(event: EmergencyEvent) -> None:
    if event.related_alarms and event.handling_started_at:
        earliest_alarm_ts = min(a.timestamp for a in event.related_alarms)
        delay_ms = (event.handling_started_at - earliest_alarm_ts) * 1000
        event.plan_execution_delay_ms = round(delay_ms, 2)


def check_and_trigger_emergency() -> List[EmergencyEvent]:
    all_alarms = _collect_all_alarms()
    triggered_events: List[EmergencyEvent] = []
    now = time.time()

    active_dedup_keys = _get_real_active_dedup_keys()

    rules = list_rules(enabled_only=True)
    for rule in rules:
        matched, affected_cranes, matched_alarms = _check_rule_match(rule, all_alarms)
        if not matched:
            continue

        dedup_key = _get_dedup_key(rule.rule_id, affected_cranes)
        if dedup_key in active_dedup_keys:
            continue

        now_str = _datetime_str(now)
        actions = _build_emergency_plan(rule.emergency_level, affected_cranes)

        event = EmergencyEvent(
            event_id=str(uuid.uuid4()),
            rule_id=rule.rule_id,
            rule_name=rule.name,
            emergency_level=rule.emergency_level,
            original_emergency_level=rule.emergency_level,
            status=EmergencyEventStatus.TRIGGERING,
            triggered_at=now,
            triggered_datetime_str=now_str,
            affected_crane_ids=list(affected_cranes),
            related_alarms=matched_alarms,
            actions=actions,
            is_drill=False,
        )

        for action in event.actions:
            _execute_action(action, event)

        event.status = EmergencyEventStatus.HANDLING
        event.handling_started_at = time.time()
        _compute_plan_execution_delay(event)

        emergency_events[event.event_id] = event
        active_dedup_keys.add(dedup_key)
        triggered_events.append(event)

        print(f"[应急响应] 触发应急事件: {event.event_id}, 规则: {rule.name}, "
              f"等级: {rule.emergency_level.value}, 涉及塔吊: {affected_cranes}")

    return triggered_events


def is_crane_blocked_by_emergency(crane_id: str) -> bool:
    for event in emergency_events.values():
        if (event.status != EmergencyEventStatus.CLOSED 
                and not event.is_drill 
                and crane_id in event.affected_crane_ids):
            return True
    return False


def get_active_emergency_events(include_drill: bool = False) -> List[EmergencyEvent]:
    events = [e for e in emergency_events.values() if e.status != EmergencyEventStatus.CLOSED]
    if not include_drill:
        events = [e for e in events if not e.is_drill]
    events.sort(key=lambda e: e.triggered_at, reverse=True)
    return events


def get_emergency_event(event_id: str) -> Optional[EmergencyEvent]:
    return emergency_events.get(event_id)


def list_emergency_events(
    level: Optional[EmergencyLevel] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    status: Optional[EmergencyEventStatus] = None,
    include_drill: bool = False,
) -> List[EmergencyEvent]:
    events = list(emergency_events.values())

    if not include_drill:
        events = [e for e in events if not e.is_drill]
    if level is not None:
        events = [e for e in events if e.emergency_level == level]
    if status is not None:
        events = [e for e in events if e.status == status]
    if start_time is not None:
        events = [e for e in events if e.triggered_at >= start_time]
    if end_time is not None:
        events = [e for e in events if e.triggered_at < end_time]

    events.sort(key=lambda e: e.triggered_at, reverse=True)
    return events


def _record_effectiveness(event: EmergencyEvent) -> None:
    if event.rule_id not in rule_effectiveness_records:
        rule_effectiveness_records[event.rule_id] = []

    action_results = []
    success_count = 0
    total_count = len(event.actions)
    has_failure = False

    for action in event.actions:
        action_result = {
            "action_type": action.action_type.value,
            "status": action.status.value,
            "execution_duration_ms": action.execution_duration_ms,
            "result_message": action.result_message,
        }
        action_results.append(action_result)
        if action.status == EmergencyActionStatus.SUCCESS:
            success_count += 1
        elif action.status == EmergencyActionStatus.FAILED:
            has_failure = True

    success_rate = success_count / total_count if total_count > 0 else 0.0

    record = RuleEffectivenessRecord(
        record_id=str(uuid.uuid4()),
        rule_id=event.rule_id,
        event_id=event.event_id,
        is_drill=event.is_drill,
        triggered_at=event.triggered_at,
        plan_execution_delay_ms=event.plan_execution_delay_ms,
        action_results=action_results,
        action_success_count=success_count,
        action_total_count=total_count,
        action_success_rate=round(success_rate, 4),
        has_action_failure=has_failure,
    )

    records = rule_effectiveness_records[event.rule_id]
    records.append(record)
    records.sort(key=lambda r: r.triggered_at, reverse=True)

    rule_effectiveness_records[event.rule_id] = records[:EFFECTIVENESS_HISTORY_WINDOW]

    _update_rule_maintenance_status(event.rule_id)


def _update_rule_maintenance_status(rule_id: str) -> None:
    rule = composite_alarm_rules.get(rule_id)
    if not rule:
        return

    records = rule_effectiveness_records.get(rule_id, [])
    real_records = [r for r in records if not r.is_drill]

    if not real_records:
        rule.needs_maintenance = False
        rule.maintenance_reason = None
        return

    recent_records = real_records[:MAINTENANCE_CHECK_WINDOW]
    has_recent_failure = any(r.has_action_failure for r in recent_records)

    if has_recent_failure:
        rule.needs_maintenance = True
        failure_events = [r.event_id for r in recent_records if r.has_action_failure]
        rule.maintenance_reason = f"最近{len(recent_records)}次真实触发中有{len(failure_events)}次出现动作失败，涉及事件: {', '.join(failure_events)}"
    else:
        rule.needs_maintenance = False
        rule.maintenance_reason = None

    rule.updated_at = time.time()


def close_emergency_event(
    event_id: str,
    closed_by: str,
    handling_result: str,
    close_reason: str,
) -> Optional[EmergencyEvent]:
    event = emergency_events.get(event_id)
    if not event:
        return None
    if event.status == EmergencyEventStatus.CLOSED:
        return event

    if not closed_by or not closed_by.strip():
        raise ValueError("关闭人不能为空")
    if not handling_result or not handling_result.strip():
        raise ValueError("处置结果不能为空")
    if not close_reason or not close_reason.strip():
        raise ValueError("关闭原因不能为空")

    if not event.is_drill:
        try:
            from scheduler import work_orders, WorkOrderStatus
            restored_count = 0
            for order in work_orders.values():
                if order.suspended_by_emergency_event_id == event_id and \
                   order.status == WorkOrderStatus.SUSPENDED:
                    if order.previous_status and order.previous_status in [
                        WorkOrderStatus.PENDING, WorkOrderStatus.ASSIGNED
                    ]:
                        order.status = order.previous_status
                    else:
                        order.status = WorkOrderStatus.PENDING
                    order.previous_status = None
                    order.suspended_by_emergency_event_id = None
                    order.failure_reason = None
                    restored_count += 1
            if restored_count > 0:
                print(f"[应急响应] 事件 {event_id} 已关闭，自动恢复了 {restored_count} 个暂停的工单")
            cleared_count = 0
            for order in work_orders.values():
                if event_id in order.affected_by_emergency_event_ids:
                    order.affected_by_emergency_event_ids.remove(event_id)
                    cleared_count += 1
            if cleared_count > 0:
                print(f"[应急响应] 事件 {event_id} 已关闭，自动清除了 {cleared_count} 个工单的受影响标记")
        except ImportError:
            pass

    now = time.time()
    event.status = EmergencyEventStatus.CLOSED
    event.closed_at = now
    event.closed_by = closed_by.strip()
    event.handling_result = handling_result.strip()
    event.close_reason = close_reason.strip()

    dedup_key = _get_dedup_key(event.rule_id, event.affected_crane_ids)
    if dedup_key in _active_rule_crane_dedup:
        del _active_rule_crane_dedup[dedup_key]

    _record_effectiveness(event)

    return event


def get_emergency_daily_stats(crane_id: str, start_ts: float, end_ts: float) -> EmergencyDailyStats:
    stats = EmergencyDailyStats()
    level_order = {EmergencyLevel.GENERAL: 1, EmergencyLevel.SERIOUS: 2, EmergencyLevel.CRITICAL: 3}

    for event in emergency_events.values():
        if event.is_drill:
            continue
        if crane_id not in event.affected_crane_ids:
            continue
        if not (start_ts <= event.triggered_at < end_ts):
            continue

        stats.emergency_event_count += 1
        if event.emergency_level == EmergencyLevel.GENERAL:
            stats.general_count += 1
        elif event.emergency_level == EmergencyLevel.SERIOUS:
            stats.serious_count += 1
        elif event.emergency_level == EmergencyLevel.CRITICAL:
            stats.critical_count += 1

        current_highest = level_order.get(stats.highest_emergency_level, 0)
        event_level = level_order.get(event.emergency_level, 0)
        if event_level > current_highest:
            stats.highest_emergency_level = event.emergency_level

    return stats


def _generate_simulated_alarms(
    rule: CompositeAlarmRule,
    target_crane_ids: Optional[List[str]] = None,
) -> Tuple[List[str], List[GenericAlarmSnapshot]]:
    now = time.time()
    now_str = _datetime_str(now)
    simulated_alarms: List[GenericAlarmSnapshot] = []
    affected_cranes: List[str] = []
    all_crane_ids = list(cranes_config.keys())

    if target_crane_ids:
        for cid in target_crane_ids:
            if cid not in all_crane_ids:
                raise ValueError(f"塔吊 {cid} 不存在")
        selected_cranes = target_crane_ids
    else:
        if rule.scope == RuleScope.SINGLE_CRANE:
            selected_cranes = [all_crane_ids[0]] if all_crane_ids else []
        elif rule.scope == RuleScope.ADJACENT_CRANES:
            adjacent_pair = None
            for i in range(len(all_crane_ids)):
                for j in range(i + 1, len(all_crane_ids)):
                    if are_cranes_adjacent(all_crane_ids[i], all_crane_ids[j]):
                        adjacent_pair = [all_crane_ids[i], all_crane_ids[j]]
                        break
                if adjacent_pair:
                    break
            selected_cranes = adjacent_pair or (all_crane_ids[:2] if len(all_crane_ids) >= 2 else all_crane_ids)
        else:
            selected_cranes = all_crane_ids[:3] if len(all_crane_ids) >= 3 else all_crane_ids

    if not selected_cranes:
        raise ValueError("没有可用的塔吊进行演练")

    affected_cranes = selected_cranes

    for condition in rule.conditions:
        alarm_count = max(condition.min_count, 1)
        for i in range(alarm_count):
            for alarm_type in condition.alarm_types:
                crane_idx = i % len(selected_cranes)
                crane_id = selected_cranes[crane_idx]
                ts = now - (condition.time_window_seconds * 0.5) + (i * 0.1)
                alarm = GenericAlarmSnapshot(
                    alarm_id=f"sim-{uuid.uuid4().hex[:8]}",
                    alarm_type=alarm_type,
                    timestamp=ts,
                    datetime_str=_datetime_str(ts),
                    crane_ids=[crane_id],
                    message=f"[演练] 模拟{alarm_type.value}告警",
                    details={
                        "simulated": True,
                        "drill_mode": True,
                        "condition_index": rule.conditions.index(condition),
                    },
                )
                simulated_alarms.append(alarm)

    simulated_alarms.sort(key=lambda x: x.timestamp)
    return affected_cranes, simulated_alarms


def _build_drill_report(
    event: EmergencyEvent,
    drill_id: str,
    initiated_by: str,
) -> DrillReport:
    success_count = 0
    failed_count = 0
    skipped_count = 0
    action_reports: List[DrillActionReport] = []

    for action in event.actions:
        report = DrillActionReport(
            action_type=action.action_type,
            target_crane_ids=list(action.target_crane_ids),
            status=action.status,
            executed_at=action.executed_at,
            execution_duration_ms=action.execution_duration_ms,
            result_message=action.result_message,
            details=dict(action.details),
        )
        action_reports.append(report)
        if action.status == EmergencyActionStatus.SUCCESS:
            success_count += 1
        elif action.status == EmergencyActionStatus.FAILED:
            failed_count += 1
        elif action.status == EmergencyActionStatus.SKIPPED:
            skipped_count += 1

    total_actions = len(event.actions)
    all_successful = total_actions > 0 and failed_count == 0 and skipped_count == 0

    drill_start = event.triggered_at
    drill_end = event.closed_at or time.time()
    total_duration_ms = round((drill_end - drill_start) * 1000, 2)

    return DrillReport(
        drill_id=drill_id,
        rule_id=event.rule_id,
        rule_name=event.rule_name,
        initiated_by=initiated_by,
        initiated_at=drill_start,
        initiated_datetime_str=event.triggered_datetime_str,
        completed_at=drill_end,
        total_duration_ms=total_duration_ms,
        plan_execution_delay_ms=event.plan_execution_delay_ms,
        affected_crane_ids=list(event.affected_crane_ids),
        simulated_alarms=list(event.related_alarms),
        action_reports=action_reports,
        success_count=success_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        all_actions_successful=all_successful,
    )


def initiate_drill(
    rule_id: str,
    initiated_by: str,
    target_crane_ids: Optional[List[str]] = None,
) -> DrillReport:
    rule = composite_alarm_rules.get(rule_id)
    if not rule:
        raise ValueError(f"规则 {rule_id} 不存在")
    if not rule.enabled:
        raise ValueError(f"规则 {rule_id} 未启用")

    affected_cranes, simulated_alarms = _generate_simulated_alarms(rule, target_crane_ids)

    now = time.time()
    now_str = _datetime_str(now)
    actions = _build_emergency_plan(rule.emergency_level, affected_cranes)

    drill_id = f"drill-{uuid.uuid4().hex[:12]}"

    event = EmergencyEvent(
        event_id=drill_id,
        rule_id=rule.rule_id,
        rule_name=rule.name,
        emergency_level=rule.emergency_level,
        original_emergency_level=rule.emergency_level,
        status=EmergencyEventStatus.TRIGGERING,
        triggered_at=now,
        triggered_datetime_str=now_str,
        affected_crane_ids=list(affected_cranes),
        related_alarms=simulated_alarms,
        actions=actions,
        is_drill=True,
        drill_initiated_by=initiated_by,
    )

    for action in event.actions:
        _execute_action(action, event)

    event.status = EmergencyEventStatus.HANDLING
    event.handling_started_at = time.time()
    _compute_plan_execution_delay(event)

    emergency_events[event.event_id] = event

    event.status = EmergencyEventStatus.CLOSED
    event.closed_at = time.time()
    event.closed_by = initiated_by
    event.handling_result = "演练完成"
    event.close_reason = "演练模式自动关闭"

    drill_report = _build_drill_report(event, drill_id, initiated_by)
    drill_reports[drill_id] = drill_report

    _record_effectiveness(event)

    print(f"[应急演练] 完成演练: {drill_id}, 规则: {rule.name}, "
          f"发起人: {initiated_by}, 涉及塔吊: {affected_cranes}, "
          f"动作成功: {drill_report.success_count}/{len(actions)}")

    return drill_report


def list_drill_reports(
    rule_id: Optional[str] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
) -> List[DrillReport]:
    reports = list(drill_reports.values())

    if rule_id is not None:
        reports = [r for r in reports if r.rule_id == rule_id]
    if start_time is not None:
        reports = [r for r in reports if r.initiated_at >= start_time]
    if end_time is not None:
        reports = [r for r in reports if r.initiated_at < end_time]

    reports.sort(key=lambda r: r.initiated_at, reverse=True)
    return reports


def get_drill_report(drill_id: str) -> Optional[DrillReport]:
    return drill_reports.get(drill_id)


def get_rule_effectiveness(rule_id: str) -> Optional[RuleEffectivenessScore]:
    rule = composite_alarm_rules.get(rule_id)
    if not rule:
        return None

    records = rule_effectiveness_records.get(rule_id, [])
    real_records = [r for r in records if not r.is_drill]

    avg_delay = None
    avg_success_rate = 0.0

    if real_records:
        delays = [r.plan_execution_delay_ms for r in real_records if r.plan_execution_delay_ms is not None]
        if delays:
            avg_delay = round(sum(delays) / len(delays), 2)

        success_rates = [r.action_success_rate for r in real_records]
        avg_success_rate = round(sum(success_rates) / len(success_rates), 4)

    return RuleEffectivenessScore(
        rule_id=rule.rule_id,
        rule_name=rule.name,
        total_real_triggers=len(real_records),
        avg_plan_execution_delay_ms=avg_delay,
        avg_action_success_rate=avg_success_rate,
        needs_maintenance=rule.needs_maintenance,
        maintenance_reason=rule.maintenance_reason,
        recent_history=list(records),
    )


def list_rules_effectiveness() -> List[RuleEffectivenessScore]:
    scores = []
    for rule_id in composite_alarm_rules.keys():
        score = get_rule_effectiveness(rule_id)
        if score:
            scores.append(score)
    return scores


def _get_next_level(current_level: EmergencyLevel) -> Optional[EmergencyLevel]:
    idx = ESCALATION_LEVEL_ORDER.index(current_level)
    if idx < len(ESCALATION_LEVEL_ORDER) - 1:
        return ESCALATION_LEVEL_ORDER[idx + 1]
    return None


def _build_supplemental_plan(
    new_level: EmergencyLevel,
    crane_ids: List[str],
    executed_action_types: Set[EmergencyActionType],
) -> List[EmergencyActionExecution]:
    full_plan = _build_emergency_plan(new_level, crane_ids)
    return [a for a in full_plan if a.action_type not in executed_action_types]


def escalate_event(
    event_id: str,
    escalation_type: str,
    reason: str,
    escalated_by: Optional[str] = None,
) -> Optional[EmergencyEvent]:
    event = emergency_events.get(event_id)
    if not event:
        return None
    if event.status != EmergencyEventStatus.HANDLING:
        return None
    if event.is_drill:
        return None

    next_level = _get_next_level(event.emergency_level)
    if not next_level:
        return None

    executed_action_types: Set[EmergencyActionType] = {
        a.action_type for a in event.actions
        if a.status in [EmergencyActionStatus.SUCCESS, EmergencyActionStatus.SKIPPED]
    }
    supplemental_actions = _build_supplemental_plan(
        next_level, event.affected_crane_ids, executed_action_types
    )

    for action in supplemental_actions:
        _execute_action(action, event)

    event.actions.extend(supplemental_actions)

    now = time.time()
    old_level = event.emergency_level
    event.emergency_level = next_level
    event.last_escalation_at = now

    log = EscalationLog(
        log_id=str(uuid.uuid4()),
        event_id=event_id,
        from_level=old_level,
        to_level=next_level,
        escalation_type=escalation_type,
        reason=reason,
        escalated_at=now,
        escalated_datetime_str=_datetime_str(now),
        escalated_by=escalated_by,
        supplemental_actions=supplemental_actions,
    )
    event.escalation_logs.append(log)

    print(f"[应急响应] 事件 {event_id} 从 {old_level.value} 升级到 {next_level.value}, "
          f"方式: {escalation_type}, 原因: {reason}, "
          f"补充动作: {[a.action_type.value for a in supplemental_actions]}")

    return event


def manual_escalate_event(
    event_id: str,
    escalated_by: str,
    reason: str,
) -> Optional[EmergencyEvent]:
    if not escalated_by or not escalated_by.strip():
        raise ValueError("提级操作人不能为空")
    if not reason or not reason.strip():
        raise ValueError("提级原因不能为空")

    event = emergency_events.get(event_id)
    if not event:
        return None
    if event.status != EmergencyEventStatus.HANDLING:
        raise ValueError(f"只有处置中的事件可以提级，当前状态: {event.status.value}")
    if event.is_drill:
        raise ValueError("演练事件不支持提级")

    next_level = _get_next_level(event.emergency_level)
    if not next_level:
        raise ValueError(f"事件已为最高等级 {event.emergency_level.value}，无法继续提级")

    return escalate_event(
        event_id=event_id,
        escalation_type="MANUAL",
        reason=reason.strip(),
        escalated_by=escalated_by.strip(),
    )


def check_auto_escalation() -> List[EmergencyEvent]:
    escalated_events: List[EmergencyEvent] = []
    now = time.time()

    for event in list(emergency_events.values()):
        if event.status != EmergencyEventStatus.HANDLING:
            continue
        if event.is_drill:
            continue
        if event.emergency_level not in ESCALATION_TIMEOUTS:
            continue

        timeout = ESCALATION_TIMEOUTS[event.emergency_level]
        reference_time = event.last_escalation_at or event.handling_started_at or event.triggered_at
        if reference_time and (now - reference_time) >= timeout:
            next_level = _get_next_level(event.emergency_level)
            if next_level:
                timeout_desc = f"{int(timeout)}秒"
                result = escalate_event(
                    event_id=event.event_id,
                    escalation_type="AUTO",
                    reason=f"处置超时自动提级: {event.emergency_level.value}级事件未在{timeout_desc}内关闭",
                )
                if result:
                    escalated_events.append(result)

    _sync_critical_affected_orders()

    return escalated_events


def _sync_critical_affected_orders() -> None:
    try:
        from scheduler import work_orders, WorkOrderStatus
    except ImportError:
        return

    active_critical_events = [
        e for e in emergency_events.values()
        if e.status == EmergencyEventStatus.HANDLING
        and not e.is_drill
        and e.emergency_level == EmergencyLevel.CRITICAL
    ]
    if not active_critical_events:
        return

    for order in work_orders.values():
        if order.status != WorkOrderStatus.EXECUTING:
            continue
        for event in active_critical_events:
            if event.event_id not in order.affected_by_emergency_event_ids:
                order.affected_by_emergency_event_ids.append(event.event_id)


def get_affected_work_orders() -> list:
    try:
        from scheduler import work_orders, WorkOrderStatus
    except ImportError:
        return []

    _sync_critical_affected_orders()

    active_statuses = {
        WorkOrderStatus.PENDING,
        WorkOrderStatus.ASSIGNED,
        WorkOrderStatus.EXECUTING,
        WorkOrderStatus.SUSPENDED,
    }
    return [o for o in work_orders.values()
            if o.affected_by_emergency_event_ids and o.status in active_statuses]
