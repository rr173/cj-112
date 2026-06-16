import time
from typing import Optional, List

from fastapi import APIRouter, HTTPException

from models import (
    ZoneArbConfig,
    TokenRequest,
    TokenReleaseRequest,
    EventType,
)
import arbiter
from arbiter import (
    overlap_sectors,
    token_statuses,
    arb_event_logs,
    cranes_held_tokens,
    cranes_pending_requests,
    cranes_config,
    clean_expired_tokens_and_waiters,
    rebuild_all_overlap_sectors,
    find_sectors_for_crane,
    get_crane_interval_for_sector,
    angle_in_interval,
    request_token,
    release_token,
    revoke_token_force,
)

router = APIRouter(prefix="/api/arb", tags=["令牌仲裁"])


@router.get("/config", summary="查询作业区域仲裁配置")
def get_arb_config():
    return arbiter.zone_arb_config


@router.put("/config", summary="更新作业区域仲裁配置")
def update_arb_config(new_config: ZoneArbConfig):
    arbiter.zone_arb_config = new_config
    return {
        "code": 0,
        "message": "配置更新成功",
        "config": arbiter.zone_arb_config,
    }


@router.post("/sectors/rebuild", summary="重新计算所有重叠扇区")
def api_rebuild_sectors():
    rebuild_all_overlap_sectors()
    return {
        "code": 0,
        "message": "重叠扇区重新计算完成",
        "sector_count": len(overlap_sectors),
        "sector_ids": list(overlap_sectors.keys()),
    }


@router.get("/sectors", summary="查询所有重叠扇区定义及令牌状态")
def list_all_sectors(with_tokens: bool = True):
    clean_expired_tokens_and_waiters()
    result = []
    for sector in overlap_sectors.values():
        item = {
            "sector": sector,
        }
        if with_tokens:
            ts = token_statuses.get(sector.sector_id)
            item["token"] = ts
        result.append(item)
    return result


@router.get("/sectors/{sector_id}", summary="查询单个重叠扇区详情")
def get_sector_detail(sector_id: str):
    clean_expired_tokens_and_waiters()
    if sector_id not in overlap_sectors:
        raise HTTPException(status_code=404, detail=f"扇区 {sector_id} 不存在")
    sector = overlap_sectors[sector_id]
    ts = token_statuses.get(sector_id)
    return {
        "sector": sector,
        "token_status": ts,
    }


@router.get("/crane/{crane_id}/sectors", summary="查询某台塔吊涉及的所有重叠扇区")
def get_crane_sectors(crane_id: str, angle_hint: Optional[float] = None):
    clean_expired_tokens_and_waiters()
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    sectors = find_sectors_for_crane(crane_id)
    result = []
    for sector in sectors:
        interval = get_crane_interval_for_sector(sector, crane_id)
        ts = token_statuses.get(sector.sector_id)
        currently_in = False
        if angle_hint is not None and interval:
            currently_in = angle_in_interval(angle_hint, interval)
        result.append({
            "sector": sector,
            "crane_interval": interval,
            "currently_in_at_angle_hint": currently_in,
            "token_status": ts,
        })
    return result


@router.post("/token/request", summary="申请重叠扇区令牌")
def api_request_token(req: TokenRequest):
    return request_token(req.crane_id, req.sector_id)


@router.post("/token/release", summary="释放重叠扇区令牌")
def api_release_token(req: TokenReleaseRequest):
    return release_token(req.crane_id, req.sector_id, req.request_id)


@router.post("/token/revoke/{sector_id}", summary="管理员强制收回某扇区令牌")
def api_revoke_token(sector_id: str, reason: Optional[str] = "admin_revoke"):
    return revoke_token_force(sector_id, reason)


@router.get("/crane/{crane_id}/tokens", summary="查询某台塔吊持有的所有令牌和待处理申请")
def get_crane_tokens(crane_id: str):
    from models import CraneTokensView
    clean_expired_tokens_and_waiters()
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")

    held: List[dict] = []
    held_set = cranes_held_tokens.get(crane_id, set())
    for sec_id in held_set:
        ts = token_statuses.get(sec_id)
        if ts:
            held.append({
                "sector_id": sec_id,
                "acquired_at": ts.acquired_at,
                "expires_at": ts.expires_at,
                "remaining_seconds": (ts.expires_at - time.time()) if ts.expires_at else 0,
                "sector": overlap_sectors.get(sec_id),
            })

    pending: List[dict] = []
    pending_map = cranes_pending_requests.get(crane_id, {})
    for sec_id, req_id in pending_map.items():
        ts = token_statuses.get(sec_id)
        pos = None
        req_at = None
        if ts:
            for idx, item in enumerate(ts.wait_queue):
                if item.request_id == req_id:
                    pos = idx + 1
                    req_at = item.requested_at
                    break
        pending.append({
            "sector_id": sec_id,
            "request_id": req_id,
            "queue_position": pos,
            "requested_at": req_at,
            "wait_elapsed_seconds": (time.time() - req_at) if req_at else 0,
            "timeout_seconds": arbiter.zone_arb_config.token_wait_timeout,
            "sector": overlap_sectors.get(sec_id),
        })

    return CraneTokensView(
        crane_id=crane_id,
        held_tokens=held,
        pending_requests=pending,
    )


@router.get("/tokens", summary="查询所有令牌状态(持有者+等待队列)")
def list_all_tokens():
    clean_expired_tokens_and_waiters()
    return list(token_statuses.values())


@router.get("/events", summary="查询仲裁事件日志(可按塔吊或扇区过滤)")
def get_arb_events(crane_id: Optional[str] = None, sector_id: Optional[str] = None,
                   event_type: Optional[EventType] = None, limit: int = 200):
    result = arb_event_logs
    if crane_id:
        result = [e for e in result if e.crane_id == crane_id]
    if sector_id:
        result = [e for e in result if e.sector_id == sector_id]
    if event_type:
        result = [e for e in result if e.event_type == event_type]
    return result[-limit:]
