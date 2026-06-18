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
    EnergyForecastDetail,
    EnergyLimitListEntry,
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

FORECAST_THRESHOLD_RATIO = 1.2
FORECAST_RECOVERY_RATIO = 1.0
MIN_SAMPLE_HOURS = 2.0
DAY_TOTAL_HOURS = 24.0

cranes_forecast_alarm_triggered: Dict[str, bool] = {}

cranes_forecast_details: Dict[str, EnergyForecastDetail] = {}

cranes_limit_list: Dict[str, EnergyLimitListEntry] = {}

cranes_limit_history: List[Dict] = []


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
        if crane_id not in cranes_forecast_alarm_triggered:
            cranes_forecast_alarm_triggered[crane_id] = False
        if crane_id not in cranes_forecast_details:
            cranes_forecast_details[crane_id] = EnergyForecastDetail(crane_id=crane_id)
    print(f"[能耗监测模块] 初始化完成，已为 {len(cranes_quota_kwh)} 台塔吊加载能耗配额")


def check_and_reset_daily():
    global _energy_current_date
    today = _get_today_date_str()
    if _energy_current_date != today:
        _record_limit_list_snapshot(_energy_current_date)
        _energy_current_date = today
        for crane_id in cranes_config.keys():
            cranes_daily_energy_kwh[crane_id] = 0.0
            cranes_energy_records[crane_id] = []
            cranes_over_limit[crane_id] = False
            cranes_yellow_alarm_triggered[crane_id] = False
            cranes_red_alarm_triggered[crane_id] = False
            cranes_forecast_alarm_triggered[crane_id] = False
            cranes_forecast_details[crane_id] = EnergyForecastDetail(crane_id=crane_id)
        cranes_limit_list.clear()
        print(f"[能耗监测模块] 零点重置完成，已清空所有塔吊当日能耗数据，日期: {today}")


def _record_limit_list_snapshot(date_str: str):
    if not cranes_limit_list:
        return
    for entry in cranes_limit_list.values():
        cranes_limit_history.append({
            "date": date_str,
            "crane_id": entry.crane_id,
            "action": "LEFT_AT_DAY_END",
            "forecast_exceed_ratio": entry.forecast_exceed_ratio,
            "joined_at": entry.joined_at,
        })


def is_crane_energy_over_limit(crane_id: str) -> bool:
    return cranes_over_limit.get(crane_id, False)


def is_crane_in_limit_list(crane_id: str) -> bool:
    return crane_id in cranes_limit_list


def get_crane_limit_hint(crane_id: str) -> Optional[Dict]:
    entry = cranes_limit_list.get(crane_id)
    if not entry:
        return None
    return {
        "in_limit_list": True,
        "forecast_exceed_ratio": round(entry.forecast_exceed_ratio, 4),
        "forecast_total_kwh": round(entry.forecast_total_kwh, 4),
        "quota_kwh": entry.quota_kwh,
        "hint": "您所在塔吊已被列入能耗预测超标限电名单，请降低作业功率或减少高能耗作业",
        "joined_at": entry.joined_at,
        "joined_datetime_str": entry.joined_datetime_str,
    }


def get_time_elapsed_today(now_ts: Optional[float] = None) -> float:
    now = datetime.fromtimestamp(now_ts) if now_ts else datetime.now()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_seconds = (now - start_of_day).total_seconds()
    return elapsed_seconds / 3600.0


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


def calculate_energy_forecast(crane_id: str, now_ts: Optional[float] = None) -> EnergyForecastDetail:
    if crane_id not in cranes_forecast_details:
        cranes_forecast_details[crane_id] = EnergyForecastDetail(crane_id=crane_id)

    detail = cranes_forecast_details[crane_id]
    detail.crane_id = crane_id
    detail.quota_kwh = cranes_quota_kwh.get(crane_id, _default_quota_kwh)
    detail.daily_consumed_kwh = cranes_daily_energy_kwh.get(crane_id, 0.0)
    detail.day_total_hours = DAY_TOTAL_HOURS

    elapsed_hours = get_time_elapsed_today(now_ts)
    detail.time_elapsed_hours = round(elapsed_hours, 4)
    detail.time_elapsed_ratio = round(elapsed_hours / DAY_TOTAL_HOURS, 4) if DAY_TOTAL_HOURS > 0 else 0.0

    is_enough = elapsed_hours >= MIN_SAMPLE_HOURS
    detail.is_enough_sample = is_enough

    if is_enough and elapsed_hours > 0:
        forecast_total = detail.daily_consumed_kwh * (DAY_TOTAL_HOURS / elapsed_hours)
        detail.forecast_total_kwh = round(forecast_total, 4)
        detail.forecast_exceed_ratio = round(forecast_total / detail.quota_kwh, 4) if detail.quota_kwh > 0 else 0.0
        detail.is_forecast_exceed = detail.forecast_exceed_ratio >= FORECAST_THRESHOLD_RATIO
    else:
        detail.forecast_total_kwh = 0.0
        detail.forecast_exceed_ratio = 0.0
        detail.is_forecast_exceed = False

    now = now_ts or time.time()
    detail.last_forecast_at = now
    detail.last_forecast_datetime_str = _datetime_str(now)

    return detail


def _create_forecast_alarm(
    crane_id: str,
    detail: EnergyForecastDetail,
    now: float,
) -> EnergyAlarmEvent:
    alarm_type = AlarmType.ENERGY_FORECAST_EXCEEDED
    message = (f"能耗预测超标告警: 塔吊 {crane_id} 当日已消耗 {detail.daily_consumed_kwh:.2f} kWh，"
               f"已过时间占比 {detail.time_elapsed_ratio*100:.1f}%({detail.time_elapsed_hours:.1f}h)，"
               f"线性外推预测当日总耗 {detail.forecast_total_kwh:.2f} kWh，"
               f"预测超标比例 {detail.forecast_exceed_ratio*100:.1f}%，已加入限电名单")

    alarm = EnergyAlarmEvent(
        alarm_id=f"ENERGY-FC-{uuid.uuid4().hex[:12].upper()}",
        alarm_type=alarm_type,
        alarm_level=EnergyAlarmLevel.FORECAST,
        crane_id=crane_id,
        timestamp=now,
        datetime_str=_datetime_str(now),
        cumulative_energy_kwh=detail.daily_consumed_kwh,
        quota_kwh=detail.quota_kwh,
        quota_usage_ratio=detail.forecast_exceed_ratio,
        message=message,
        details={
            "forecast_total_kwh": detail.forecast_total_kwh,
            "time_elapsed_hours": detail.time_elapsed_hours,
            "time_elapsed_ratio": detail.time_elapsed_ratio,
            "forecast_exceed_ratio": detail.forecast_exceed_ratio,
            "alarm_type": "FORECAST",
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
                "cumulative_energy_kwh": detail.daily_consumed_kwh,
                "quota_kwh": detail.quota_kwh,
                "forecast_total_kwh": detail.forecast_total_kwh,
                "forecast_exceed_ratio": detail.forecast_exceed_ratio,
                "time_elapsed_hours": detail.time_elapsed_hours,
                "alarm_level": EnergyAlarmLevel.FORECAST.value,
            },
        )
        alarm_history.append(generic_alarm)
    except Exception as e:
        print(f"[能耗监测模块] 写入预测告警历史失败: {e}")

    return alarm


def _add_crane_to_limit_list(crane_id: str, detail: EnergyForecastDetail, now: float,
                             reason: str = "FORECAST_EXCEED", operator: Optional[str] = None) -> EnergyLimitListEntry:
    config = cranes_config.get(crane_id)
    entry = EnergyLimitListEntry(
        crane_id=crane_id,
        crane_name=config.name if config else crane_id,
        joined_at=now,
        joined_datetime_str=_datetime_str(now),
        forecast_exceed_ratio=detail.forecast_exceed_ratio,
        forecast_total_kwh=detail.forecast_total_kwh,
        daily_consumed_kwh=detail.daily_consumed_kwh,
        quota_kwh=detail.quota_kwh,
        time_elapsed_ratio=detail.time_elapsed_ratio,
        join_reason=reason,
        is_manual_override=operator is not None,
        override_by=operator,
    )
    cranes_limit_list[crane_id] = entry

    cranes_limit_history.append({
        "date": _get_today_date_str(),
        "crane_id": crane_id,
        "action": "JOIN",
        "reason": reason,
        "forecast_exceed_ratio": detail.forecast_exceed_ratio,
        "joined_at": now,
        "operator": operator,
    })

    print(f"[能耗监测模块] 塔吊 {crane_id} 加入限电名单，预测超标: {detail.forecast_exceed_ratio*100:.1f}%")
    return entry


def _remove_crane_from_limit_list(crane_id: str, now: float, reason: str = "FORECAST_RECOVERY",
                                  operator: Optional[str] = None) -> bool:
    if crane_id not in cranes_limit_list:
        return False

    entry = cranes_limit_list.pop(crane_id)

    cranes_limit_history.append({
        "date": _get_today_date_str(),
        "crane_id": crane_id,
        "action": "LEAVE",
        "reason": reason,
        "forecast_exceed_ratio": entry.forecast_exceed_ratio,
        "joined_at": entry.joined_at,
        "left_at": now,
        "operator": operator,
    })

    try:
        from models import AlarmEvent
        recovery_alarm = AlarmEvent(
            alarm_id=f"ENERGY-RC-{uuid.uuid4().hex[:12].upper()}",
            alarm_type=AlarmType.ENERGY_LIMIT_RECOVERY,
            timestamp=now,
            datetime_str=_datetime_str(now),
            crane_a_id=crane_id,
            crane_b_id="",
            message=(f"能耗预测恢复: 塔吊 {crane_id} 预测值已回落到配额100%以下，"
                     f"已从限电名单移除，移除原因: {reason}"),
            details={
                "crane_id": crane_id,
                "join_reason": entry.join_reason,
                "leave_reason": reason,
                "joined_at": entry.joined_at,
                "left_at": now,
                "operator": operator,
            },
        )
        alarm_history.append(recovery_alarm)
    except Exception as e:
        print(f"[能耗监测模块] 写入恢复事件历史失败: {e}")

    print(f"[能耗监测模块] 塔吊 {crane_id} 从限电名单移除，原因: {reason}")
    return True


def manually_remove_crane_from_limit_list(crane_id: str, operator: Optional[str] = None,
                                          reason: Optional[str] = None) -> Dict:
    if crane_id not in cranes_config:
        raise ValueError(f"塔吊 {crane_id} 不存在")

    now = time.time()
    remove_reason = reason or "MANUAL_OVERRIDE"
    removed = _remove_crane_from_limit_list(crane_id, now, remove_reason, operator)

    return {
        "success": True,
        "removed": removed,
        "crane_id": crane_id,
        "operator": operator,
        "reason": remove_reason,
        "timestamp": now,
        "datetime_str": _datetime_str(now),
    }


def get_limit_list() -> List[EnergyLimitListEntry]:
    return list(cranes_limit_list.values())


def get_forecast_detail(crane_id: str) -> Optional[EnergyForecastDetail]:
    if crane_id not in cranes_config:
        return None
    return calculate_energy_forecast(crane_id)


def _check_energy_forecast(crane_id: str, cumulative_kwh: float, now: float):
    detail = calculate_energy_forecast(crane_id, now)
    was_in_list = crane_id in cranes_limit_list

    if detail.is_enough_sample and detail.is_forecast_exceed:
        if not cranes_forecast_alarm_triggered.get(crane_id, False):
            _create_forecast_alarm(crane_id, detail, now)
            cranes_forecast_alarm_triggered[crane_id] = True

        if not was_in_list:
            _add_crane_to_limit_list(crane_id, detail, now)

    if was_in_list and detail.is_enough_sample and detail.forecast_exceed_ratio < FORECAST_RECOVERY_RATIO:
        _remove_crane_from_limit_list(crane_id, now, "FORECAST_RECOVERY")


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
    if crane_id not in cranes_forecast_alarm_triggered:
        cranes_forecast_alarm_triggered[crane_id] = False
    if crane_id not in cranes_forecast_details:
        cranes_forecast_details[crane_id] = EnergyForecastDetail(crane_id=crane_id)
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
    _check_energy_forecast(crane_id, report.cumulative_energy_kwh, now)

    forecast_detail = cranes_forecast_details.get(crane_id)
    limit_hint = get_crane_limit_hint(crane_id)

    result = {
        "recorded": True,
        "crane_id": crane_id,
        "instantaneous_power_kw": report.instantaneous_power_kw,
        "daily_cumulative_kwh": report.cumulative_energy_kwh,
        "is_over_limit": cranes_over_limit.get(crane_id, False),
        "alarm_triggered": False,
        "forecast": forecast_detail.model_dump() if forecast_detail else None,
        "limit_hint": limit_hint,
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
        in_limit_list = crane_id in cranes_limit_list
        forecast_detail = calculate_energy_forecast(crane_id)
        ranking.append({
            "crane_id": crane_id,
            "crane_name": config.name if config else crane_id,
            "daily_cumulative_kwh": round(daily_cumulative, 4),
            "quota_kwh": cranes_quota_kwh.get(crane_id, _default_quota_kwh),
            "is_over_limit": cranes_over_limit.get(crane_id, False),
            "is_in_limit_list": in_limit_list,
            "forecast_total_kwh": round(forecast_detail.forecast_total_kwh, 4) if forecast_detail else 0.0,
            "forecast_exceed_ratio": round(forecast_detail.forecast_exceed_ratio, 4) if forecast_detail else 0.0,
        })
    ranking.sort(key=lambda x: x["daily_cumulative_kwh"], reverse=True)
    return ranking


def get_energy_daily_stats(crane_id: str, start_ts: float, end_ts: float) -> EnergyDailyStats:
    stats = EnergyDailyStats()
    alarms = [a for a in cranes_energy_alarms
              if a.crane_id == crane_id and start_ts <= a.timestamp < end_ts]
    stats.yellow_alarm_count = sum(1 for a in alarms if a.alarm_level == EnergyAlarmLevel.YELLOW)
    stats.red_alarm_count = sum(1 for a in alarms if a.alarm_level == EnergyAlarmLevel.RED)
    stats.forecast_alarm_count = sum(1 for a in alarms if a.alarm_level == EnergyAlarmLevel.FORECAST)
    stats.over_limit = cranes_over_limit.get(crane_id, False)

    today_date = _get_today_date_str()
    limit_events = [e for e in cranes_limit_history
                    if e["crane_id"] == crane_id and e.get("date") == today_date]
    stats.limit_recovery_count = sum(1 for e in limit_events if e.get("action") == "LEAVE")
    stats.was_in_limit_list = crane_id in cranes_limit_list or len(limit_events) > 0

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
    forecast_alarms = sum(1 for a in cranes_energy_alarms if a.alarm_level == EnergyAlarmLevel.FORECAST)
    over_limit_count = sum(1 for v in cranes_over_limit.values() if v)
    limit_list_count = len(cranes_limit_list)
    total_records = sum(len(records) for records in cranes_energy_records.values())

    return {
        "total_energy_alarms": total_alarms,
        "yellow_alarm_count": yellow_alarms,
        "red_alarm_count": red_alarms,
        "forecast_alarm_count": forecast_alarms,
        "over_limit_cranes": over_limit_count,
        "limit_list_cranes": limit_list_count,
        "total_meter_records_cached": total_records,
        "default_daily_quota_kwh": _default_quota_kwh,
    }
