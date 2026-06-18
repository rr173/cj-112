import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from models import (
    CooperativeLiftTask,
    CooperativeLiftCreate,
    CooperativeLiftStatus,
    CraneLiftAssignment,
    HeightDesyncAlarm,
)
from collision import cranes_config, lock_crane, cranes_current_status, can_crane_reach_point, cranes_lock_status


cooperative_tasks: Dict[str, CooperativeLiftTask] = {}
crane_task_map: Dict[str, List[str]] = {}


def _format_datetime(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def init_cooperative_lift_module():
    for crane_id in cranes_config:
        if crane_id not in crane_task_map:
            crane_task_map[crane_id] = []


def _has_active_orders(crane_id: str) -> bool:
    try:
        from scheduler import work_orders, WorkOrderStatus
        for order in work_orders.values():
            if order.assigned_crane_id == crane_id and order.status in (
                WorkOrderStatus.ASSIGNED,
                WorkOrderStatus.EXECUTING,
            ):
                return True
    except ImportError:
        pass
    return False


def _has_active_cooperative_task(crane_id: str) -> bool:
    task_ids = crane_task_map.get(crane_id, [])
    for task_id in task_ids:
        task = cooperative_tasks.get(task_id)
        if task and task.status in (
            CooperativeLiftStatus.PENDING_READY,
            CooperativeLiftStatus.SYNCHRONIZING,
            CooperativeLiftStatus.EXECUTING,
        ):
            return True
    return False


def create_cooperative_task(create: CooperativeLiftCreate) -> Dict:
    if len(create.crane_assignments) < 2:
        return {"error": "协同吊装任务至少需要2台塔吊参与"}

    crane_ids = [a.crane_id for a in create.crane_assignments]
    if len(crane_ids) != len(set(crane_ids)):
        return {"error": "参与塔吊不能重复"}

    for assignment in create.crane_assignments:
        if assignment.crane_id not in cranes_config:
            return {"error": f"塔吊 {assignment.crane_id} 不存在"}
        if assignment.load_ratio <= 0 or assignment.load_ratio > 1:
            return {"error": f"塔吊 {assignment.crane_id} 的载荷比例必须在 (0, 1] 之间"}

    total_ratio = sum(a.load_ratio for a in create.crane_assignments)
    if abs(total_ratio - 1.0) > 0.001:
        return {"error": f"所有塔吊载荷比例之和必须等于100%，当前为 {total_ratio * 100:.1f}%"}

    overload_cranes = []
    for assignment in create.crane_assignments:
        config = cranes_config[assignment.crane_id]
        assigned_load = create.component.weight * assignment.load_ratio
        max_allowed = config.max_load * 0.85
        if assigned_load > max_allowed:
            overload_cranes.append({
                "crane_id": assignment.crane_id,
                "crane_name": config.name,
                "assigned_load": round(assigned_load, 3),
                "max_allowed": round(max_allowed, 3),
                "max_load": config.max_load,
                "overload_by": round(assigned_load - max_allowed, 3),
            })

    if overload_cranes:
        return {
            "error": "部分塔吊承担载荷超过其最大起重量的85%",
            "overload_cranes": overload_cranes,
        }

    busy_cranes = []
    for assignment in create.crane_assignments:
        cid = assignment.crane_id
        if _has_active_orders(cid):
            busy_cranes.append({"crane_id": cid, "reason": "存在未完成的普通工单"})
        elif _has_active_cooperative_task(cid):
            busy_cranes.append({"crane_id": cid, "reason": "正在参与其他协同吊装任务"})
        elif cranes_lock_status.get(cid) and cranes_lock_status[cid].is_locked:
            lock_reason = cranes_lock_status[cid].locked_reason or "未知原因"
            busy_cranes.append({"crane_id": cid, "reason": f"塔吊处于锁定状态({lock_reason})"})
        else:
            try:
                from wind_speed_monitor import is_crane_wind_shutdown
                if is_crane_wind_shutdown(cid):
                    busy_cranes.append({"crane_id": cid, "reason": "处于风速停机状态"})
            except ImportError:
                pass

    if busy_cranes:
        return {
            "error": "部分塔吊当前有未完成任务或处于锁定状态，无法加入协同吊装",
            "busy_cranes": busy_cranes,
        }

    unreachable_cranes = []
    for assignment in create.crane_assignments:
        cid = assignment.crane_id
        config = cranes_config[cid]
        issues = []
        if not can_crane_reach_point(cid, create.lift_x, create.lift_y):
            issues.append("无法到达起吊点")
        if not can_crane_reach_point(cid, create.drop_x, create.drop_y):
            issues.append("无法到达落点")
        if issues:
            unreachable_cranes.append({
                "crane_id": cid,
                "crane_name": config.name,
                "issues": issues,
                "lift_x": create.lift_x,
                "lift_y": create.lift_y,
                "drop_x": create.drop_x,
                "drop_y": create.drop_y,
            })

    if unreachable_cranes:
        return {
            "error": "部分塔吊无法到达起吊点或落点",
            "unreachable_cranes": unreachable_cranes,
        }

    now = time.time()
    task_id = f"COOP-{uuid.uuid4().hex[:8].upper()}"

    task = CooperativeLiftTask(
        task_id=task_id,
        component=create.component,
        crane_assignments=create.crane_assignments,
        initiator=create.initiator,
        lift_x=create.lift_x,
        lift_y=create.lift_y,
        drop_x=create.drop_x,
        drop_y=create.drop_y,
        estimated_duration=create.estimated_duration,
        height_diff_threshold=create.height_diff_threshold,
        height_diff_duration_threshold=create.height_diff_duration_threshold,
        status=CooperativeLiftStatus.PENDING_READY,
        created_at=now,
        updated_at=now,
    )

    cooperative_tasks[task_id] = task

    for assignment in create.crane_assignments:
        cid = assignment.crane_id
        if cid not in crane_task_map:
            crane_task_map[cid] = []
        crane_task_map[cid].append(task_id)

    return {
        "task": task,
        "message": f"协同吊装任务创建成功，任务ID: {task_id}，当前状态: 待就绪",
    }


def _validate_operator_for_crane(operator_id: str, crane_id: str) -> Optional[str]:
    try:
        from operator_training import can_operator_operate_crane, operator_crane_bindings
    except ImportError:
        return None

    check = can_operator_operate_crane(operator_id, crane_id)
    if not check.get("can_operate"):
        return check.get("reason", "操作员无操作资格")

    bound_crane = operator_crane_bindings.get(operator_id)
    if bound_crane != crane_id:
        if bound_crane:
            return f"操作员当前绑定塔吊 {bound_crane}，未绑定塔吊 {crane_id}"
        else:
            return f"操作员未绑定任何塔吊，无法操作塔吊 {crane_id}"

    return None


def confirm_ready(task_id: str, crane_id: str, operator_id: str) -> Dict:
    task = cooperative_tasks.get(task_id)
    if not task:
        return {"error": f"协同吊装任务 {task_id} 不存在"}

    if task.status != CooperativeLiftStatus.PENDING_READY:
        return {"error": f"任务当前状态为 {task.status.value}，无法确认就绪"}

    assignment_crane_ids = [a.crane_id for a in task.crane_assignments]
    if crane_id not in assignment_crane_ids:
        return {"error": f"塔吊 {crane_id} 未参与此协同吊装任务"}

    op_error = _validate_operator_for_crane(operator_id, crane_id)
    if op_error:
        return {"error": f"操作员校验失败: {op_error}"}

    if crane_id in task.ready_cranes:
        return {
            "task": task,
            "message": f"塔吊 {crane_id} 已确认就绪，无需重复确认",
            "already_confirmed": True,
        }

    task.ready_cranes.append(crane_id)
    task.updated_at = time.time()

    all_ready = len(task.ready_cranes) == len(task.crane_assignments)
    if all_ready:
        task.status = CooperativeLiftStatus.SYNCHRONIZING
        task.sync_command_sent_at = time.time()
        task.updated_at = time.time()
        return {
            "task": task,
            "message": "所有参与塔吊均已确认就绪，任务进入同步中状态，同步起吊指令已下发",
            "all_ready": True,
            "entered_synchronizing": True,
        }

    return {
        "task": task,
        "message": f"塔吊 {crane_id} 已确认就绪，还需 {len(task.crane_assignments) - len(task.ready_cranes)} 台塔吊确认",
        "all_ready": False,
        "ready_count": len(task.ready_cranes),
        "total_count": len(task.crane_assignments),
    }


def ack_sync_command(task_id: str, crane_id: str, operator_id: str) -> Dict:
    task = cooperative_tasks.get(task_id)
    if not task:
        return {"error": f"协同吊装任务 {task_id} 不存在"}

    if task.status != CooperativeLiftStatus.SYNCHRONIZING:
        return {"error": f"任务当前状态为 {task.status.value}，无法确认同步指令"}

    assignment_crane_ids = [a.crane_id for a in task.crane_assignments]
    if crane_id not in assignment_crane_ids:
        return {"error": f"塔吊 {crane_id} 未参与此协同吊装任务"}

    op_error = _validate_operator_for_crane(operator_id, crane_id)
    if op_error:
        return {"error": f"操作员校验失败: {op_error}"}

    if crane_id in task.sync_acked_cranes:
        return {
            "task": task,
            "message": f"塔吊 {crane_id} 已确认收到同步指令，无需重复确认",
            "already_acked": True,
        }

    task.sync_acked_cranes.append(crane_id)
    task.updated_at = time.time()

    all_acked = len(task.sync_acked_cranes) == len(task.crane_assignments)
    if all_acked:
        task.status = CooperativeLiftStatus.EXECUTING
        task.started_at = time.time()
        task.updated_at = time.time()
        return {
            "task": task,
            "message": "所有参与塔吊均已确认收到同步指令，任务进入执行中状态",
            "all_acked": True,
            "entered_executing": True,
        }

    return {
        "task": task,
        "message": f"塔吊 {crane_id} 已确认收到同步指令，还需 {len(task.crane_assignments) - len(task.sync_acked_cranes)} 台塔吊确认",
        "all_acked": False,
        "acked_count": len(task.sync_acked_cranes),
        "total_count": len(task.crane_assignments),
    }


def confirm_complete(task_id: str, initiator: str) -> Dict:
    task = cooperative_tasks.get(task_id)
    if not task:
        return {"error": f"协同吊装任务 {task_id} 不存在"}

    if task.status != CooperativeLiftStatus.EXECUTING:
        return {"error": f"任务当前状态为 {task.status.value}，无法确认完成"}

    if initiator != task.initiator:
        return {"error": "只有任务发起人可以确认完成"}

    now = time.time()
    task.status = CooperativeLiftStatus.COMPLETED
    task.completed_at = now
    task.updated_at = now

    return {
        "task": task,
        "message": "协同吊装任务已完成",
    }


def abort_task(task_id: str, reason: str) -> Optional[CooperativeLiftTask]:
    task = cooperative_tasks.get(task_id)
    if not task:
        return None

    if task.status in (CooperativeLiftStatus.COMPLETED, CooperativeLiftStatus.ABORTED):
        return task

    now = time.time()
    task.status = CooperativeLiftStatus.ABORTED
    task.aborted_at = now
    task.abort_reason = reason
    task.updated_at = now

    for assignment in task.crane_assignments:
        cid = assignment.crane_id
        lock_crane(cid, f"协同吊装任务 {task_id} 中止: {reason}")
        if cid not in task.locked_cranes:
            task.locked_cranes.append(cid)

    return task


def get_task(task_id: str) -> Optional[CooperativeLiftTask]:
    return cooperative_tasks.get(task_id)


def get_all_tasks(status: Optional[CooperativeLiftStatus] = None) -> List[CooperativeLiftTask]:
    tasks = list(cooperative_tasks.values())
    if status:
        tasks = [t for t in tasks if t.status == status]
    tasks.sort(key=lambda t: t.created_at, reverse=True)
    return tasks


def get_crane_tasks(crane_id: str) -> List[CooperativeLiftTask]:
    task_ids = crane_task_map.get(crane_id, [])
    tasks = [cooperative_tasks[tid] for tid in task_ids if tid in cooperative_tasks]
    tasks.sort(key=lambda t: t.created_at, reverse=True)
    return tasks


_height_exceed_start: Dict[str, Dict] = {}


def check_height_desync(task: CooperativeLiftTask) -> Optional[HeightDesyncAlarm]:
    if task.status not in (CooperativeLiftStatus.SYNCHRONIZING, CooperativeLiftStatus.EXECUTING):
        return None

    crane_ids = [a.crane_id for a in task.crane_assignments]

    hook_heights = {}
    for cid in crane_ids:
        status = cranes_current_status.get(cid)
        if status:
            hook_heights[cid] = status.hook_height
        else:
            return None

    max_diff = 0.0
    max_pair = (None, None)
    for i in range(len(crane_ids)):
        for j in range(i + 1, len(crane_ids)):
            diff = abs(hook_heights[crane_ids[i]] - hook_heights[crane_ids[j]])
            if diff > max_diff:
                max_diff = diff
                max_pair = (crane_ids[i], crane_ids[j])

    key = f"{task.task_id}_height_desync"
    now = time.time()

    if max_diff > task.height_diff_threshold:
        if key not in _height_exceed_start:
            _height_exceed_start[key] = {
                "start_time": now,
                "crane_a": max_pair[0],
                "crane_b": max_pair[1],
                "max_diff": max_diff,
            }
        else:
            record = _height_exceed_start[key]
            duration = now - record["start_time"]
            if max_diff > record["max_diff"]:
                record["max_diff"] = max_diff
                record["crane_a"] = max_pair[0]
                record["crane_b"] = max_pair[1]

            if duration >= task.height_diff_duration_threshold:
                alarm = HeightDesyncAlarm(
                    alarm_id=f"HD-{uuid.uuid4().hex[:8].upper()}",
                    task_id=task.task_id,
                    timestamp=now,
                    datetime_str=_format_datetime(now),
                    crane_a_id=record["crane_a"],
                    crane_b_id=record["crane_b"],
                    height_diff=round(record["max_diff"], 3),
                    threshold=task.height_diff_threshold,
                    duration=round(duration, 2),
                )
                task.height_desync_alarms.append(alarm)
                del _height_exceed_start[key]
                return alarm
    else:
        if key in _height_exceed_start:
            del _height_exceed_start[key]

    return None


def process_crane_status_for_cooperative(crane_id: str) -> List[Dict]:
    results = []
    task_ids = crane_task_map.get(crane_id, [])

    for task_id in task_ids:
        task = cooperative_tasks.get(task_id)
        if not task:
            continue
        if task.status not in (CooperativeLiftStatus.SYNCHRONIZING, CooperativeLiftStatus.EXECUTING):
            continue

        alarm = check_height_desync(task)
        if alarm:
            aborted_task = abort_task(
                task_id,
                f"高度失同步告警: {alarm.crane_a_id} 与 {alarm.crane_b_id} 吊钩高度差 {alarm.height_diff}米，超过阈值 {alarm.threshold}米，持续 {alarm.duration}秒"
            )
            results.append({
                "task_id": task_id,
                "alarm": alarm,
                "aborted": aborted_task is not None,
            })

    return results


def handle_crane_lock_for_cooperative(crane_id: str, lock_reason: str):
    task_ids = crane_task_map.get(crane_id, [])
    for task_id in task_ids:
        task = cooperative_tasks.get(task_id)
        if not task:
            continue
        if task.status in (
            CooperativeLiftStatus.PENDING_READY,
            CooperativeLiftStatus.SYNCHRONIZING,
            CooperativeLiftStatus.EXECUTING,
        ):
            abort_task(
                task_id,
                f"参与塔吊 {crane_id} 被锁定({lock_reason})，协同任务自动中止"
            )


def is_crane_in_active_cooperative_task(crane_id: str) -> bool:
    return _has_active_cooperative_task(crane_id)
