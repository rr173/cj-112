import math
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

from models import (
    ZoneArbConfig,
    AngleInterval,
    OverlapSector,
    WaitQueueItem,
    TokenStatus,
    EventType,
    ArbEventLog,
    CraneTokenRejectionInfo,
)
from collision import (
    cranes_config,
    get_pair_key,
    normalize_angle,
    angle_in_interval,
    make_angle_interval,
    compute_bearing,
)

zone_arb_config = ZoneArbConfig()

overlap_sectors: Dict[str, OverlapSector] = {}
sector_pair_index: Dict[str, str] = {}
token_statuses: Dict[str, TokenStatus] = {}
arb_event_logs: List[ArbEventLog] = []
cranes_held_tokens: Dict[str, set] = {}
cranes_pending_requests: Dict[str, Dict[str, str]] = {}
cranes_token_rejection: Dict[str, CraneTokenRejectionInfo] = {}


def log_arb_event(event_type: EventType, crane_id: Optional[str] = None,
                  sector_id: Optional[str] = None, details: Optional[Dict] = None):
    now = time.time()
    event = ArbEventLog(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        timestamp=now,
        datetime_str=datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        crane_id=crane_id,
        sector_id=sector_id,
        details=details or {},
    )
    arb_event_logs.append(event)


def compute_overlap_interval(bearing_to_other_deg: float,
                             self_arm: float,
                             other_arm: float,
                             distance: float) -> Optional[AngleInterval]:
    if distance >= self_arm + other_arm:
        return None
    if distance + min(self_arm, other_arm) <= max(self_arm, other_arm):
        inner = max(self_arm, other_arm) - distance
        half_span = math.degrees(math.acos(max(-1.0, min(1.0, inner / self_arm)))) if self_arm > 0 else 0
        return make_angle_interval(bearing_to_other_deg - half_span, bearing_to_other_deg + half_span)
    cos_alpha = (self_arm ** 2 + distance ** 2 - other_arm ** 2) / (2 * self_arm * distance)
    cos_alpha = max(-1.0, min(1.0, cos_alpha))
    half_span = math.degrees(math.acos(cos_alpha))
    return make_angle_interval(bearing_to_other_deg - half_span, bearing_to_other_deg + half_span)


def detect_overlap_sectors_for_pair(a_id: str, b_id: str) -> Optional[OverlapSector]:
    a = cranes_config.get(a_id)
    b = cranes_config.get(b_id)
    if not a or not b:
        return None

    dx = b.tower_x - a.tower_x
    dy = b.tower_y - a.tower_y
    dist = math.sqrt(dx * dx + dy * dy)

    interval_a = compute_overlap_interval(
        bearing_to_other_deg=compute_bearing(a.tower_x, a.tower_y, b.tower_x, b.tower_y),
        self_arm=a.arm_length,
        other_arm=b.arm_length,
        distance=dist,
    )
    if interval_a is None:
        return None

    interval_b = compute_overlap_interval(
        bearing_to_other_deg=compute_bearing(b.tower_x, b.tower_y, a.tower_x, a.tower_y),
        self_arm=b.arm_length,
        other_arm=a.arm_length,
        distance=dist,
    )

    sorted_ids = sorted([a_id, b_id])
    sector_id = f"SEC-{get_pair_key(a_id, b_id)}"
    crane_a_id_sorted = sorted_ids[0]
    crane_b_id_sorted = sorted_ids[1]
    sector = OverlapSector(
        sector_id=sector_id,
        crane_a_id=crane_a_id_sorted,
        crane_b_id=crane_b_id_sorted,
        crane_a_interval=interval_a if a_id == crane_a_id_sorted else interval_b,
        crane_b_interval=interval_b if b_id == crane_b_id_sorted else interval_a,
        distance_between_towers=dist,
        created_at=time.time(),
    )
    return sector


def rebuild_all_overlap_sectors():
    global overlap_sectors, sector_pair_index
    overlap_sectors.clear()
    sector_pair_index.clear()

    crane_ids = list(cranes_config.keys())
    for i in range(len(crane_ids)):
        for j in range(i + 1, len(crane_ids)):
            a_id, b_id = crane_ids[i], crane_ids[j]
            sector = detect_overlap_sectors_for_pair(a_id, b_id)
            if sector:
                overlap_sectors[sector.sector_id] = sector
                sector_pair_index[get_pair_key(a_id, b_id)] = sector.sector_id
                if sector.sector_id not in token_statuses:
                    token_statuses[sector.sector_id] = TokenStatus(sector_id=sector.sector_id)
                log_arb_event(EventType.SECTOR_DETECTED, sector_id=sector.sector_id, details={
                    "crane_a": sector.crane_a_id,
                    "crane_b": sector.crane_b_id,
                    "distance": sector.distance_between_towers,
                })


def get_crane_interval_for_sector(sector: OverlapSector, crane_id: str) -> Optional[AngleInterval]:
    if crane_id == sector.crane_a_id:
        return sector.crane_a_interval
    if crane_id == sector.crane_b_id:
        return sector.crane_b_interval
    return None


def find_sectors_for_crane_and_angle(crane_id: str, angle: float) -> List[Tuple[OverlapSector, AngleInterval]]:
    result = []
    for sector in overlap_sectors.values():
        if crane_id not in (sector.crane_a_id, sector.crane_b_id):
            continue
        interval = get_crane_interval_for_sector(sector, crane_id)
        if interval and angle_in_interval(angle, interval):
            result.append((sector, interval))
    return result


def find_sectors_for_crane(crane_id: str) -> List[OverlapSector]:
    result = []
    for sector in overlap_sectors.values():
        if crane_id in (sector.crane_a_id, sector.crane_b_id):
            result.append(sector)
    return result


def find_sector_ids_for_crane_at_angles(crane_id: str, angles: List[float]) -> List[str]:
    sector_ids = set()
    for angle in angles:
        for sector, _ in find_sectors_for_crane_and_angle(crane_id, angle):
            sector_ids.add(sector.sector_id)
    return list(sector_ids)


def clean_expired_tokens_and_waiters():
    now = time.time()
    for sector_id, ts in list(token_statuses.items()):
        if ts.holder_crane_id and ts.expires_at and now > ts.expires_at:
            prev_holder = ts.holder_crane_id
            prev_acquired = ts.acquired_at
            log_arb_event(EventType.TOKEN_REVOKED, crane_id=prev_holder, sector_id=sector_id, details={
                "reason": "hold_timeout",
                "acquired_at": prev_acquired,
                "expires_at": ts.expires_at,
            })
            if prev_holder in cranes_held_tokens:
                cranes_held_tokens[prev_holder].discard(sector_id)
            ts.holder_crane_id = None
            ts.acquired_at = None
            ts.expires_at = None
            _try_grant_token_to_next(sector_id)

        valid_queue: List[WaitQueueItem] = []
        for item in ts.wait_queue:
            if now - item.requested_at > zone_arb_config.token_wait_timeout:
                log_arb_event(EventType.TOKEN_REQUEST_TIMEOUT, crane_id=item.crane_id, sector_id=sector_id, details={
                    "request_id": item.request_id,
                    "requested_at": item.requested_at,
                    "timeout_seconds": zone_arb_config.token_wait_timeout,
                })
                if item.crane_id in cranes_pending_requests:
                    cranes_pending_requests[item.crane_id].pop(sector_id, None)
            else:
                valid_queue.append(item)
        ts.wait_queue = valid_queue


def _try_grant_token_to_next(sector_id: str):
    ts = token_statuses.get(sector_id)
    if not ts or ts.holder_crane_id:
        return
    if not ts.wait_queue:
        return
    next_item = ts.wait_queue.pop(0)
    now = time.time()
    ts.holder_crane_id = next_item.crane_id
    ts.acquired_at = now
    ts.expires_at = now + zone_arb_config.token_max_hold_time
    if next_item.crane_id not in cranes_held_tokens:
        cranes_held_tokens[next_item.crane_id] = set()
    cranes_held_tokens[next_item.crane_id].add(sector_id)
    if next_item.crane_id in cranes_pending_requests:
        cranes_pending_requests[next_item.crane_id].pop(sector_id, None)
    log_arb_event(EventType.TOKEN_ACQUIRED, crane_id=next_item.crane_id, sector_id=sector_id, details={
        "request_id": next_item.request_id,
        "from_queue": True,
        "expires_at": ts.expires_at,
    })
    log_arb_event(EventType.TOKEN_DEQUEUED, crane_id=next_item.crane_id, sector_id=sector_id, details={
        "request_id": next_item.request_id,
    })


def request_token(crane_id: str, sector_id: str) -> Dict:
    clean_expired_tokens_and_waiters()
    if crane_id not in cranes_config:
        raise HTTPException(status_code=404, detail=f"塔吊 {crane_id} 不存在")
    if sector_id not in overlap_sectors:
        raise HTTPException(status_code=404, detail=f"扇区 {sector_id} 不存在")
    sector = overlap_sectors[sector_id]
    if crane_id not in (sector.crane_a_id, sector.crane_b_id):
        raise HTTPException(status_code=400, detail=f"塔吊 {crane_id} 不属于扇区 {sector_id}")

    try:
        from maintenance import check_all_windows, is_crane_in_maintenance
        check_all_windows()
        if is_crane_in_maintenance(crane_id):
            return {
                "granted": False,
                "request_id": None,
                "sector_id": sector_id,
                "crane_id": crane_id,
                "queued": False,
                "message": "塔吊处于维保停机中，令牌申请已被拒绝",
                "code": "MAINTENANCE_SHUTDOWN",
            }
    except ImportError:
        pass

    ts = token_statuses[sector_id]

    if ts.holder_crane_id == crane_id:
        return {
            "granted": True,
            "request_id": None,
            "sector_id": sector_id,
            "crane_id": crane_id,
            "message": "已持有该扇区令牌",
            "expires_at": ts.expires_at,
        }

    if crane_id in cranes_pending_requests and sector_id in cranes_pending_requests[crane_id]:
        req_id = cranes_pending_requests[crane_id][sector_id]
        for item in ts.wait_queue:
            if item.request_id == req_id:
                return {
                    "granted": False,
                    "request_id": req_id,
                    "sector_id": sector_id,
                    "crane_id": crane_id,
                    "queued": True,
                    "message": "令牌已在等待队列中",
                    "queue_position": ts.wait_queue.index(item) + 1,
                }

    request_id = str(uuid.uuid4())

    if ts.holder_crane_id is None:
        now = time.time()
        ts.holder_crane_id = crane_id
        ts.acquired_at = now
        ts.expires_at = now + zone_arb_config.token_max_hold_time
        if crane_id not in cranes_held_tokens:
            cranes_held_tokens[crane_id] = set()
        cranes_held_tokens[crane_id].add(sector_id)
        log_arb_event(EventType.TOKEN_ACQUIRED, crane_id=crane_id, sector_id=sector_id, details={
            "request_id": request_id,
            "from_queue": False,
            "expires_at": ts.expires_at,
        })
        return {
            "granted": True,
            "request_id": request_id,
            "sector_id": sector_id,
            "crane_id": crane_id,
            "message": "令牌授予成功",
            "expires_at": ts.expires_at,
        }
    else:
        item = WaitQueueItem(crane_id=crane_id, request_id=request_id, requested_at=time.time())
        ts.wait_queue.append(item)
        if crane_id not in cranes_pending_requests:
            cranes_pending_requests[crane_id] = {}
        cranes_pending_requests[crane_id][sector_id] = request_id
        log_arb_event(EventType.TOKEN_ENQUEUED, crane_id=crane_id, sector_id=sector_id, details={
            "request_id": request_id,
            "current_holder": ts.holder_crane_id,
            "queue_position": len(ts.wait_queue),
        })
        return {
            "granted": False,
            "request_id": request_id,
            "sector_id": sector_id,
            "crane_id": crane_id,
            "queued": True,
            "message": "令牌已被占用，已进入等待队列",
            "queue_position": len(ts.wait_queue),
            "wait_timeout_seconds": zone_arb_config.token_wait_timeout,
        }


def release_token(crane_id: str, sector_id: str, request_id: Optional[str] = None) -> Dict:
    clean_expired_tokens_and_waiters()
    if sector_id not in token_statuses:
        raise HTTPException(status_code=404, detail=f"扇区 {sector_id} 不存在")
    ts = token_statuses[sector_id]
    if ts.holder_crane_id != crane_id:
        still_pending = False
        if crane_id in cranes_pending_requests and sector_id in cranes_pending_requests[crane_id]:
            pending_req_id = cranes_pending_requests[crane_id][sector_id]
            ts.wait_queue = [i for i in ts.wait_queue if i.request_id != pending_req_id]
            cranes_pending_requests[crane_id].pop(sector_id, None)
            still_pending = True
        if not still_pending:
            return {
                "released": False,
                "sector_id": sector_id,
                "crane_id": crane_id,
                "message": f"塔吊 {crane_id} 未持有扇区 {sector_id} 的令牌",
            }
        else:
            log_arb_event(EventType.TOKEN_DEQUEUED, crane_id=crane_id, sector_id=sector_id, details={
                "request_id": request_id or pending_req_id,
                "reason": "voluntary_withdraw",
            })
            return {
                "released": True,
                "from_queue": True,
                "sector_id": sector_id,
                "crane_id": crane_id,
                "message": "已取消排队申请",
            }

    log_arb_event(EventType.TOKEN_RELEASED, crane_id=crane_id, sector_id=sector_id, details={
        "request_id": request_id,
        "held_for_seconds": time.time() - (ts.acquired_at or 0),
    })

    if crane_id in cranes_held_tokens:
        cranes_held_tokens[crane_id].discard(sector_id)

    ts.holder_crane_id = None
    ts.acquired_at = None
    ts.expires_at = None

    _try_grant_token_to_next(sector_id)

    return {
        "released": True,
        "sector_id": sector_id,
        "crane_id": crane_id,
        "message": "令牌释放成功",
    }


def revoke_token_force(sector_id: str, reason: str = "admin_revoke") -> Dict:
    clean_expired_tokens_and_waiters()
    if sector_id not in token_statuses:
        raise HTTPException(status_code=404, detail=f"扇区 {sector_id} 不存在")
    ts = token_statuses[sector_id]
    if not ts.holder_crane_id:
        return {
            "code": 0,
            "message": "该扇区当前没有持有令牌的塔吊",
            "sector_id": sector_id,
        }
    holder = ts.holder_crane_id
    log_arb_event(EventType.TOKEN_REVOKED, crane_id=holder, sector_id=sector_id, details={
        "reason": reason,
        "acquired_at": ts.acquired_at,
    })
    if holder in cranes_held_tokens:
        cranes_held_tokens[holder].discard(sector_id)
    ts.holder_crane_id = None
    ts.acquired_at = None
    ts.expires_at = None
    _try_grant_token_to_next(sector_id)
    return {
        "code": 0,
        "message": "令牌已强制收回",
        "sector_id": sector_id,
        "previous_holder": holder,
        "granted_to_next": token_statuses[sector_id].holder_crane_id,
    }
