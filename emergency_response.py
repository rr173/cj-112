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
)
from collision import cranes_config, alarm_history, lock_crane as collision_lock_crane


composite_alarm_rules: Dict[str, CompositeAlarmRule] = {}
emergency_events: Dict[str, EmergencyEvent] = {}
_active_rule_crane_dedup: Dict[Tuple[str, str], str] = {}


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
            if type_counts.get(atype, 0) < condition.min_count:
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
            action_type=EmergencyActionType.TRIGGER_BROADCAST,
            target_crane_ids=list(crane_ids),
        ))

    return actions


def _execute_action(action: EmergencyActionExecution, event: EmergencyEvent) -> None:
    now = time.time()
    try:
        if action.action_type == EmergencyActionType.LOCK_CRANE:
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
            action.status = EmergencyActionStatus.SUCCESS
            action.result_message = f"已通知{role}: 应急事件 {event.event_id}, 等级 {event.emergency_level.value}"
            action.details = {
                "event_id": event.event_id,
                "emergency_level": event.emergency_level.value,
                "affected_cranes": action.target_crane_ids,
                "notified_at": now,
            }

        elif action.action_type == EmergencyActionType.SUSPEND_WORK_ORDERS:
            try:
                from scheduler import work_orders, WorkOrderStatus
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
            action.status = EmergencyActionStatus.SUCCESS
            action.result_message = f"全场广播已触发: 应急事件 {event.event_id}, 等级 {event.emergency_level.value}"
            action.details = {
                "event_id": event.event_id,
                "emergency_level": event.emergency_level.value,
                "broadcast_content": f"紧急广播：触发{event.emergency_level.value}级应急事件，请相关人员立即处置！涉及塔吊: {', '.join(action.target_crane_ids)}",
                "broadcast_at": now,
            }

    except Exception as e:
        action.status = EmergencyActionStatus.FAILED
        action.result_message = f"执行失败: {str(e)}"

    action.executed_at = now


def _get_dedup_key(rule_id: str, crane_ids: List[str]) -> Tuple[str, str]:
    return (rule_id, "|".join(sorted(set(crane_ids))))


def check_and_trigger_emergency() -> List[EmergencyEvent]:
    all_alarms = _collect_all_alarms()
    triggered_events: List[EmergencyEvent] = []
    now = time.time()

    active_events = [e for e in emergency_events.values() if e.status != EmergencyEventStatus.CLOSED]
    active_dedup_keys: Set[Tuple[str, str]] = set()
    for event in active_events:
        active_dedup_keys.add(_get_dedup_key(event.rule_id, event.affected_crane_ids))

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
            status=EmergencyEventStatus.TRIGGERING,
            triggered_at=now,
            triggered_datetime_str=now_str,
            affected_crane_ids=list(affected_cranes),
            related_alarms=matched_alarms,
            actions=actions,
        )

        for action in event.actions:
            _execute_action(action, event)

        event.status = EmergencyEventStatus.HANDLING
        event.handling_started_at = time.time()

        emergency_events[event.event_id] = event
        active_dedup_keys.add(dedup_key)
        triggered_events.append(event)

        print(f"[应急响应] 触发应急事件: {event.event_id}, 规则: {rule.name}, "
              f"等级: {rule.emergency_level.value}, 涉及塔吊: {affected_cranes}")

    return triggered_events


def is_crane_blocked_by_emergency(crane_id: str) -> bool:
    for event in emergency_events.values():
        if event.status != EmergencyEventStatus.CLOSED and crane_id in event.affected_crane_ids:
            return True
    return False


def get_active_emergency_events() -> List[EmergencyEvent]:
    events = [e for e in emergency_events.values() if e.status != EmergencyEventStatus.CLOSED]
    events.sort(key=lambda e: e.triggered_at, reverse=True)
    return events


def get_emergency_event(event_id: str) -> Optional[EmergencyEvent]:
    return emergency_events.get(event_id)


def list_emergency_events(
    level: Optional[EmergencyLevel] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    status: Optional[EmergencyEventStatus] = None,
) -> List[EmergencyEvent]:
    events = list(emergency_events.values())

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

    return event


def get_emergency_daily_stats(crane_id: str, start_ts: float, end_ts: float) -> EmergencyDailyStats:
    stats = EmergencyDailyStats()
    level_order = {EmergencyLevel.GENERAL: 1, EmergencyLevel.SERIOUS: 2, EmergencyLevel.CRITICAL: 3}

    for event in emergency_events.values():
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
