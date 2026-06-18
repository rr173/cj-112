import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from models import (
    EnergyAlarmEvent,
    EnergyAlarmLevel,
    EnergyCraneStatus,
    EnergyDailyStats,
    EnergyMeterRecord,
    EnergyMeterReport,
    AlarmType,
)
from collision import cranes_config, alarm_history


DEFAULT_DAILY_QUOTA_KWH = 500.0

_default_quota_kwh: float = DEFAULT_DAILY_QUOTA_KWH

cranes_quota_kwh: Dict[str, float] = {}

cranes_daily_energy_kwh: Dict[str, float] = {}

cranes_energy_records: Dict[str, List[EnergyMeterRecord]] = {}

cranes_energy_alarms: List[EnergyAlarmEvent] = []

cranes_over_limit: Dict[str, bool] = {}

cranes_yellow_alarm_triggered: Dict[str, bool] = {}

cranes_red_alarm_triggered: Dict[str, bool] = {}

_energy_current_date: str = ""


def _datetime_str(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _get_today_date_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def init_energy_monitor_module():
    global _energy_current_date
    _energy_current_date = _get_today_date_str()
    for crane_id in cranes_config.keys():
        if crane_id not in cranes_quota_kwh:
            cranes_quota_kwh[crane_id] = _default_quota_kwh
        if crane_id not in cranes_daily_energy_kwh:
            cranes_daily_energy_kwh[crane_id] = 0.0
        if crane_id not in cranes_energy_records:
            cranes_energy_records[crane_id] = []
        if crane_id not in cranes_over_limit:
            cranes_over_limit[crane_id] = False
        if crane_id not in cranes_yellow_alarm_triggered:
            cranes_yellow_alarm_triggered[crane_id] = False
        if crane_id not in cranes_red_alarm_triggered:
            cranes_red_alarm_triggered[crane_id] = False
    print(f"[能耗监测模块] 初始化完成，已为 {len(cranes_quota_kwh)} 台塔吊加载能耗配额")


def check_and_reset_daily():
    global _energy_current_date
    today = _get_today_date_str()
    if _energy_current_date != today:
        _energy_current_date = today
        for crane_id in cranes_config.keys():
            cranes_daily_energy_kwh[crane_id] = 0.0
            cranes_energy_records[crane_id] = []
            cranes_over_limit[crane_id] = False
            cranes_yellow_alarm_triggered[crane_id] = False
            cranes_red_alarm_triggered[crane_id] = False
        print(f"[能耗监测模块] 零点重置完成，已清空所有塔吊当日能耗数据，日期: {today}")


def is_crane_energy_over_limit(crane_id: str) -> bool:
    return cranes_over_limit.get(crane_id, False)


def get_crane_energy_over_limit_amount(crane_id: str) -> float:
    daily = cranes_daily_energy_kwh.get(crane_id, 0.0)
    quota = cranes_quota_kwh.get(crane_id, _default_quota_kwh)
    if daily > quota:
        return daily - quota
    return 0.0


def _get_current_executing_order(crane_id: str) -> Optional[Dict]:
    try:
        from scheduler import work_orders, WorkOrderStatus
        for order in work_orders.values():
            if (order.assigned_crane_id == crane_id and
                    order.status == WorkOrderStatus.EXECUTING):
                return order
    except ImportError:
        pass
    return None


def calculate_efficiency_ratio(crane_id: str) -> Optional[float]:
    order = _get_current_executing_order(crane_id)
    if not order:
        return None
    weight_tons = order.weight
    if weight_tons <= 0:
        return None
    records = cranes_energy_records.get(crane_id, [])
    if not records:
        return None
    energy_since_order_start = 0.0
    if order.started_at:
        relevant_records = [r for r in records if r.sensor_timestamp >= order.started_at]
        if relevant_records:
            energy_at_start = relevant_records[0].cumulative_energy_kwh
            energy_at_end = relevant_records[-1].cumulative_energy_kwh
            energy_since_order_start = energy_at_end - energy_at_start
    if energy_since_order_start <= 0:
        return None
    return round(energy_since_order_start / weight_tons, 4)


def _create_energy_alarm(
    crane_id: str,
    alarm_level: EnergyAlarmLevel,
    cumulative_kwh: float,
    quota_kwh: float,
    message: str,
    now: float,
) -> EnergyAlarmEvent:
    alarm_type = (AlarmType.ENERGY_QUOTA_EXCEEDED
                  if alarm_level == EnergyAlarmLevel.RED
                  else AlarmType.ENERGY_QUOTA_WARNING)
    usage_ratio = cumulative_kwh / quota_kwh if quota_kwh > 0 else 0.0

    alarm = EnergyAlarmEvent(
        alarm_id=f"ENERGY-{uuid.uuid4().hex[:12].upper()}",
        alarm_type=alarm_type,
        alarm_level=alarm_level,
        crane_id=crane_id,
        timestamp=now,
        datetime_str=_datetime_str(now),
        cumulative_energy_kwh=cumulative_kwh,
        quota_kwh=quota_kwh,
        quota_usage_ratio=round(usage_ratio, 4),
        message=message,
        details={
            "default_quota": _default_quota_kwh,
            "crane_quota": quota_kwh,
        },
    )

    cranes_energy_alarms.append(alarm)

    try:
        from models import AlarmEvent
        generic_alarm = AlarmEvent(
            alarm_id=alarm.alarm_id,
            alarm_type=alarm_type,
            timestamp=alarm.timestamp,
            datetime_str=alarm.datetime_str,
            crane_a_id=crane_id,
            crane_b_id="",
            message=alarm.message,
            details={
                "cumulative_energy_kwh": cumulative_kwh,
                "quota_kwh": quota_kwh,
                "quota_usage_ratio": round(usage_ratio, 4),
                "alarm_level": alarm_level.value,
            },
        )
        alarm_history.append(generic_alarm)
    except Exception as e:
        print(f"[能耗监测模块] 写入通用告警历史失败: {e}")

    return alarm


def _check_energy_alarms(crane_id: str, cumulative_kwh: float, now: float):
    quota = cranes_quota_kwh.get(crane_id, _default_quota_kwh)
    usage_ratio = cumulative_kwh / quota if quota > 0 else 0.0

    if usage_ratio >= 1.0:
        if not cranes_red_alarm_triggered.get(crane_id, False):
            message = (f"能耗超限告警: 塔吊 {crane_id} 当日累计能耗 {cumulative_kwh:.2f} kWh "
                       f"已达到日配额 {quota:.0f} kWh 的100%，标记为能耗超限状态")
            _create_energy_alarm(
                crane_id=crane_id,
                alarm_level=EnergyAlarmLevel.RED,
                cumulative_kwh=cumulative_kwh,
                quota_kwh=quota,
                message=message,
                now=now,
            )
            cranes_red_alarm_triggered[crane_id] = True
            cranes_over_limit[crane_id] = True
            print(f"[能耗监测模块] 塔吊 {crane_id} 能耗超限(红色告警)，累计: {cumulative_kwh:.2f} kWh，配额: {quota:.0f} kWh")

    elif usage_ratio >= 0.8:
        if not cranes_yellow_alarm_triggered.get(crane_id, False):
            message = (f"能耗预警: 塔吊 {crane_id} 当日累计能耗 {cumulative_kwh:.2f} kWh "
                       f"已达到日配额 {quota:.0f} kWh 的80%")
            _create_energy_alarm(
                crane_id=crane_id,
                alarm_level=EnergyAlarmLevel.YELLOW,
                cumulative_kwh=cumulative_kwh,
                quota_kwh=quota,
                message=message,
                now=now,
            )
            cranes_yellow_alarm_triggered[crane_id] = True
            print(f"[能耗监测模块] 塔吊 {crane_id} 能耗预警(黄色)，累计: {cumulative_kwh:.2f} kWh，配额: {quota:.0f} kWh")


def process_energy_meter_report(report: EnergyMeterReport) -> Dict:
    if report.crane_id not in cranes_config:
        raise ValueError(f"塔吊 {report.crane_id} 不存在")
    if report.instantaneous_power_kw < 0:
        raise ValueError("瞬时功率不能为负数")
    if report.cumulative_energy_kwh < 0:
        raise ValueError("累计电量不能为负数")

    check_and_reset_daily()

    now = time.time()
    crane_id = report.crane_id

    if crane_id not in cranes_energy_records:
        cranes_energy_records[crane_id] = []
    if crane_id not in cranes_daily_energy_kwh:
        cranes_daily_energy_kwh[crane_id] = 0.0
    if crane_id not in cranes_over_limit:
        cranes_over_limit[crane_id] = False
    if crane_id not in cranes_yellow_alarm_triggered:
        cranes_yellow_alarm_triggered[crane_id] = False
    if crane_id not in cranes_red_alarm_triggered:
        cranes_red_alarm_triggered[crane_id] = False
    if crane_id not in cranes_quota_kwh:
        cranes_quota_kwh[crane_id] = _default_quota_kwh

    energy_record = EnergyMeterRecord(
        crane_id=crane_id,
        instantaneous_power_kw=report.instantaneous_power_kw,
        cumulative_energy_kwh=report.cumulative_energy_kwh,
        sensor_timestamp=report.sensor_timestamp,
        received_at=now,
        datetime_str=_datetime_str(report.sensor_timestamp),
    )
    cranes_energy_records[crane_id].append(energy_record)

    cranes_daily_energy_kwh[crane_id] = report.cumulative_energy_kwh

    _check_energy_alarms(crane_id, report.cumulative_energy_kwh, now)

    result = {
        "recorded": True,
        "crane_id": crane_id,
        "instantaneous_power_kw": report.instantaneous_power_kw,
        "daily_cumulative_kwh": report.cumulative_energy_kwh,
        "is_over_limit": cranes_over_limit.get(crane_id, False),
        "alarm_triggered": False,
    }

    if cranes_energy_alarms and cranes_energy_alarms[-1].crane_id == crane_id and now - cranes_energy_alarms[-1].timestamp < 1.0:
        result["alarm_triggered"] = True
        result["latest_alarm"] = cranes_energy_alarms[-1]

    return result


def get_energy_status(crane_id: str) -> Optional[EnergyCraneStatus]:
    if crane_id not in cranes_config:
        return None

    check_and_reset_daily()

    quota = cranes_quota_kwh.get(crane_id, _default_quota_kwh)
    daily_cumulative = cranes_daily_energy_kwh.get(crane_id, 0.0)
    records = cranes_energy_records.get(crane_id, [])
    latest_record = records[-1] if records else None

    efficiency = calculate_efficiency_ratio(crane_id)

    current_order = _get_current_executing_order(crane_id)
    current_order_id = current_order.order_id if current_order else None
    current_weight = current_order.weight if current_order else None

    quota_remaining = max(0.0, quota - daily_cumulative)
    usage_ratio = daily_cumulative / quota if quota > 0 else 0.0

    return EnergyCraneStatus(
        crane_id=crane_id,
        instantaneous_power_kw=latest_record.instantaneous_power_kw if latest_record else None,
        daily_cumulative_kwh=round(daily_cumulative, 4),
        quota_kwh=quota,
        quota_remaining_kwh=round(quota_remaining, 4),
        quota_usage_ratio=round(usage_ratio, 4),
        efficiency_ratio=efficiency,
        is_over_limit=cranes_over_limit.get(crane_id, False),
        latest_sensor_timestamp=latest_record.sensor_timestamp if latest_record else None,
        latest_datetime_str=latest_record.datetime_str if latest_record else None,
        current_work_order_id=current_order_id,
        current_work_weight_tons=current_weight,
    )


def get_all_energy_statuses() -> List[EnergyCraneStatus]:
    check_and_reset_daily()
    result = []
    for crane_id in cranes_config.keys():
        status = get_energy_status(crane_id)
        if status:
            result.append(status)
    return result


def get_energy_history(
    crane_id: str,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    limit: int = 500,
) -> List[EnergyMeterRecord]:
    if crane_id not in cranes_energy_records:
        return []
    records = list(cranes_energy_records[crane_id])
    if start_time is not None:
        records = [r for r in records if r.sensor_timestamp >= start_time]
    if end_time is not None:
        records = [r for r in records if r.sensor_timestamp <= end_time]
    return records[-limit:]


def get_energy_alarm_history(
    crane_id: Optional[str] = None,
    alarm_level: Optional[EnergyAlarmLevel] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    limit: int = 200,
) -> List[EnergyAlarmEvent]:
    result = cranes_energy_alarms
    if crane_id:
        result = [a for a in result if a.crane_id == crane_id]
    if alarm_level:
        result = [a for a in result if a.alarm_level == alarm_level]
    if start_time is not None:
        result = [a for a in result if a.timestamp >= start_time]
    if end_time is not None:
        result = [a for a in result if a.timestamp <= end_time]
    return result[-limit:]


def update_energy_quota(
    crane_id: Optional[str] = None,
    daily_quota_kwh: Optional[float] = None,
) -> Dict:
    global _default_quota_kwh

    if daily_quota_kwh is not None:
        if daily_quota_kwh <= 0:
            raise ValueError("每日能耗配额必须大于0")

    if crane_id is not None:
        if crane_id not in cranes_config:
            raise ValueError(f"塔吊 {crane_id} 不存在")
        if daily_quota_kwh is not None:
            cranes_quota_kwh[crane_id] = daily_quota_kwh
        target_desc = f"塔吊 {crane_id}"
    else:
        if daily_quota_kwh is not None:
            _default_quota_kwh = daily_quota_kwh
            for cid in cranes_quota_kwh:
                cranes_quota_kwh[cid] = daily_quota_kwh
        target_desc = "全局默认配置及所有塔吊"

    print(f"[能耗监测模块] {target_desc} 日能耗配额已热更新为 {daily_quota_kwh} kWh")

    return {
        "success": True,
        "updated_target": target_desc,
        "current_quota": cranes_quota_kwh.get(crane_id, _default_quota_kwh) if crane_id else _default_quota_kwh,
    }


def get_energy_ranking() -> List[Dict]:
    check_and_reset_daily()
    ranking = []
    for crane_id in cranes_config.keys():
        daily_cumulative = cranes_daily_energy_kwh.get(crane_id, 0.0)
        config = cranes_config.get(crane_id)
        ranking.append({
            "crane_id": crane_id,
            "crane_name": config.name if config else crane_id,
            "daily_cumulative_kwh": round(daily_cumulative, 4),
            "quota_kwh": cranes_quota_kwh.get(crane_id, _default_quota_kwh),
            "is_over_limit": cranes_over_limit.get(crane_id, False),
        })
    ranking.sort(key=lambda x: x["daily_cumulative_kwh"], reverse=True)
    return ranking


def get_energy_daily_stats(crane_id: str, start_ts: float, end_ts: float) -> EnergyDailyStats:
    stats = EnergyDailyStats()
    alarms = [a for a in cranes_energy_alarms
              if a.crane_id == crane_id and start_ts <= a.timestamp < end_ts]
    stats.yellow_alarm_count = sum(1 for a in alarms if a.alarm_level == EnergyAlarmLevel.YELLOW)
    stats.red_alarm_count = sum(1 for a in alarms if a.alarm_level == EnergyAlarmLevel.RED)
    stats.over_limit = cranes_over_limit.get(crane_id, False)

    records = cranes_energy_records.get(crane_id, [])
    day_records = [r for r in records if start_ts <= r.sensor_timestamp < end_ts]
    if day_records:
        stats.total_energy_kwh = day_records[-1].cumulative_energy_kwh
        stats.peak_power_kw = max(r.instantaneous_power_kw for r in day_records)
        if len(day_records) > 1:
            avg_power = sum(r.instantaneous_power_kw for r in day_records) / len(day_records)
            stats.avg_power_kw = round(avg_power, 4)

    return stats


def get_energy_stats() -> Dict:
    total_alarms = len(cranes_energy_alarms)
    yellow_alarms = sum(1 for a in cranes_energy_alarms if a.alarm_level == EnergyAlarmLevel.YELLOW)
    red_alarms = sum(1 for a in cranes_energy_alarms if a.alarm_level == EnergyAlarmLevel.RED)
    over_limit_count = sum(1 for v in cranes_over_limit.values() if v)
    total_records = sum(len(records) for records in cranes_energy_records.values())

    return {
        "total_energy_alarms": total_alarms,
        "yellow_alarm_count": yellow_alarms,
        "red_alarm_count": red_alarms,
        "over_limit_cranes": over_limit_count,
        "total_meter_records_cached": total_records,
        "default_daily_quota_kwh": _default_quota_kwh,
    }
