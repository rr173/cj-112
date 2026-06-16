import time

from fastapi import FastAPI

from models import TowerCraneConfig, LockStatus
from collision import (
    cranes_config,
    cranes_lock_status,
    pair_safety_thresholds,
    DEFAULT_SAFETY_THRESHOLD,
    get_pair_key,
)
from arbiter import (
    cranes_held_tokens,
    cranes_pending_requests,
    token_statuses,
    clean_expired_tokens_and_waiters,
    rebuild_all_overlap_sectors,
)
from routes_crane import router as crane_router
from routes_arb import router as arb_router
from routes_order import router as order_router

app = FastAPI(title="塔吊防碰撞联锁服务", description="建筑工地多塔吊防碰撞实时监测系统")

app.include_router(crane_router)
app.include_router(arb_router)
app.include_router(order_router)


@app.on_event("startup")
def init_cranes():
    preset_cranes = [
        TowerCraneConfig(
            crane_id="CRANE-001",
            name="1号塔吊",
            tower_x=0.0,
            tower_y=0.0,
            tower_z=50.0,
            arm_length=60.0,
            max_load=10.0,
            min_angle=0.0,
            max_angle=360.0,
        ),
        TowerCraneConfig(
            crane_id="CRANE-002",
            name="2号塔吊",
            tower_x=40.0,
            tower_y=30.0,
            tower_z=55.0,
            arm_length=55.0,
            max_load=8.0,
            min_angle=0.0,
            max_angle=360.0,
        ),
        TowerCraneConfig(
            crane_id="CRANE-003",
            name="3号塔吊",
            tower_x=-35.0,
            tower_y=45.0,
            tower_z=48.0,
            arm_length=50.0,
            max_load=12.0,
            min_angle=45.0,
            max_angle=315.0,
        ),
    ]
    for c in preset_cranes:
        cranes_config[c.crane_id] = c
        cranes_lock_status[c.crane_id] = LockStatus(
            crane_id=c.crane_id, is_locked=False
        )
        cranes_held_tokens[c.crane_id] = set()
        cranes_pending_requests[c.crane_id] = {}

    crane_ids = list(cranes_config.keys())
    for i in range(len(crane_ids)):
        for j in range(i + 1, len(crane_ids)):
            pair_safety_thresholds[get_pair_key(crane_ids[i], crane_ids[j])] = DEFAULT_SAFETY_THRESHOLD

    rebuild_all_overlap_sectors()


@app.get("/health", summary="健康检查")
def health_check():
    clean_expired_tokens_and_waiters()
    from collision import cranes_current_status, alarm_history, cranes_lock_status
    from arbiter import overlap_sectors, token_statuses
    return {
        "status": "ok",
        "service": "塔吊防碰撞联锁服务",
        "cranes_registered": len(cranes_config),
        "total_alarms": len(alarm_history),
        "locked_cranes": sum(1 for l in cranes_lock_status.values() if l.is_locked),
        "overlap_sectors": len(overlap_sectors),
        "active_tokens": sum(1 for t in token_statuses.values() if t.holder_crane_id),
        "timestamp": time.time(),
    }
