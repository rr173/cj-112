import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import (
    LoadMomentEnvelopePoint,
    WeightSensorReport,
    OverloadAlarmLevel,
    TowerCraneConfig,
    CraneStatus,
    LockStatus,
)
from collision import (
    cranes_config,
    cranes_current_status,
    cranes_lock_status,
)
from load_moment_monitor import (
    validate_envelope_points,
    calculate_allowed_load,
    determine_alarm_level,
    process_weight_report,
    set_envelope_curve,
    get_weight_history,
    get_overload_alarm_history,
    get_realtime_load_status,
    cranes_weight_records,
    cranes_envelope_curves,
    cranes_overload_alarms,
    MAX_WEIGHT_RECORDS_PER_CRANE,
    init_load_moment_monitor_module,
)


def _setup_test_env():
    cranes_config.clear()
    cranes_current_status.clear()
    cranes_lock_status.clear()
    cranes_envelope_curves.clear()
    cranes_weight_records.clear()
    cranes_overload_alarms.clear()

    cranes_config["TEST-CRANE-001"] = TowerCraneConfig(
        crane_id="TEST-CRANE-001",
        name="测试塔吊1号",
        tower_x=0.0,
        tower_y=0.0,
        tower_z=50.0,
        arm_length=60.0,
        max_load=10.0,
        min_angle=0.0,
        max_angle=360.0,
    )
    cranes_lock_status["TEST-CRANE-001"] = LockStatus(
        crane_id="TEST-CRANE-001",
        is_locked=False,
    )
    init_load_moment_monitor_module()


def test_validate_envelope_points():
    print("=== 测试1: 包络曲线数据验证 ===")

    good_points = [
        LoadMomentEnvelopePoint(distance=5, max_load=10),
        LoadMomentEnvelopePoint(distance=15, max_load=8.5),
        LoadMomentEnvelopePoint(distance=30, max_load=5.8),
        LoadMomentEnvelopePoint(distance=45, max_load=3.8),
        LoadMomentEnvelopePoint(distance=60, max_load=2.5),
    ]
    err = validate_envelope_points(good_points)
    assert err is None, f"有效数据不应报错: {err}"
    print("  ✓ 5个有效采样点验证通过")

    too_few = good_points[:4]
    err = validate_envelope_points(too_few)
    assert err is not None and "至少需要5个" in err
    print(f"  ✓ 少于5个点正确报错: {err}")

    dup_distance = good_points + [LoadMomentEnvelopePoint(distance=30, max_load=5.5)]
    err = validate_envelope_points(dup_distance)
    assert err is not None and "严格递增" in err
    print(f"  ✓ 重复距离正确报错: {err}")

    increasing_load = [
        LoadMomentEnvelopePoint(distance=5, max_load=10),
        LoadMomentEnvelopePoint(distance=15, max_load=12),
        LoadMomentEnvelopePoint(distance=30, max_load=8),
        LoadMomentEnvelopePoint(distance=45, max_load=5),
        LoadMomentEnvelopePoint(distance=60, max_load=3),
    ]
    err = validate_envelope_points(increasing_load)
    assert err is not None and "递减" in err
    print(f"  ✓ 载荷递增异常正确报错: {err}")

    negative = [
        LoadMomentEnvelopePoint(distance=-5, max_load=10),
        LoadMomentEnvelopePoint(distance=15, max_load=8),
        LoadMomentEnvelopePoint(distance=30, max_load=6),
        LoadMomentEnvelopePoint(distance=45, max_load=4),
        LoadMomentEnvelopePoint(distance=60, max_load=2),
    ]
    err = validate_envelope_points(negative)
    assert err is not None and "正数" in err
    print(f"  ✓ 负距离正确报错: {err}")


def test_calculate_allowed_load_linear_interpolation():
    print("\n=== 测试2: 力矩包络曲线线性插值计算 ===")
    _setup_test_env()

    envelope = cranes_envelope_curves["TEST-CRANE-001"]
    print(f"  使用的包络曲线采样点: {[(p.distance, p.max_load) for p in envelope]}")

    p0 = envelope[0]
    p4 = envelope[-1]
    allowed_at_p0 = calculate_allowed_load("TEST-CRANE-001", p0.distance)
    assert abs(allowed_at_p0 - p0.max_load) < 0.001
    print(f"  ✓ 变幅{p0.distance}m处允许载荷: {allowed_at_p0} 吨 (精确匹配采样点)")

    allowed_at_p4 = calculate_allowed_load("TEST-CRANE-001", p4.distance)
    assert abs(allowed_at_p4 - p4.max_load) < 0.001
    print(f"  ✓ 变幅{p4.distance}m处允许载荷: {allowed_at_p4} 吨 (精确匹配采样点)")

    p_start = envelope[0]
    p_end = envelope[1]
    mid_distance = (p_start.distance + p_end.distance) / 2
    allowed_mid = calculate_allowed_load("TEST-CRANE-001", mid_distance)
    expected = p_start.max_load + (p_end.max_load - p_start.max_load) * (mid_distance - p_start.distance) / (p_end.distance - p_start.distance)
    assert abs(allowed_mid - expected) < 0.001
    print(f"  ✓ 变幅{mid_distance}m处({p_start.distance}m-{p_end.distance}m之间)允许载荷: {allowed_mid:.4f} 吨, 期望值: {expected:.4f}")

    p2 = envelope[2]
    p3 = envelope[3]
    mid23_dist = (p2.distance + p3.distance) / 2
    allowed_mid23 = calculate_allowed_load("TEST-CRANE-001", mid23_dist)
    expected_23 = p2.max_load + (p3.max_load - p2.max_load) * (mid23_dist - p2.distance) / (p3.distance - p2.distance)
    assert abs(allowed_mid23 - expected_23) < 0.001
    print(f"  ✓ 变幅{mid23_dist}m处({p2.distance}m-{p3.distance}m之间)允许载荷: {allowed_mid23:.4f} 吨, 期望值: {expected_23:.4f}")

    below_min = p0.distance / 2
    allowed_below = calculate_allowed_load("TEST-CRANE-001", below_min)
    assert abs(allowed_below - p0.max_load) < 0.001
    print(f"  ✓ 变幅{below_min}m(小于最小采样点)允许载荷: {allowed_below} 吨 (取最小点值)")

    above_max = p4.distance * 1.2
    allowed_above = calculate_allowed_load("TEST-CRANE-001", above_max)
    assert abs(allowed_above - p4.max_load) < 0.001
    print(f"  ✓ 变幅{above_max}m(大于最大采样点)允许载荷: {allowed_above} 吨 (取最大点值)")

    unknown_crane = calculate_allowed_load("UNKNOWN-CRANE", 30.0)
    assert unknown_crane is None
    print("  ✓ 未知塔吊返回 None")


def test_determine_alarm_level():
    print("\n=== 测试3: 三级告警分级判定 ===")

    assert determine_alarm_level(0.5) is None
    print("  ✓ 50% 超载比例: 无告警")

    assert determine_alarm_level(0.89) is None
    print("  ✓ 89% 超载比例: 无告警")

    assert determine_alarm_level(0.90) == OverloadAlarmLevel.YELLOW
    print("  ✓ 90% 超载比例: 黄色预警")

    assert determine_alarm_level(0.95) == OverloadAlarmLevel.YELLOW
    print("  ✓ 95% 超载比例: 黄色预警")

    assert determine_alarm_level(0.99) == OverloadAlarmLevel.YELLOW
    print("  ✓ 99% 超载比例: 黄色预警")

    assert determine_alarm_level(1.00) == OverloadAlarmLevel.ORANGE
    print("  ✓ 100% 超载比例: 橙色告警")

    assert determine_alarm_level(1.05) == OverloadAlarmLevel.ORANGE
    print("  ✓ 105% 超载比例: 橙色告警")

    assert determine_alarm_level(1.09) == OverloadAlarmLevel.ORANGE
    print("  ✓ 109% 超载比例: 橙色告警")

    assert determine_alarm_level(1.10) == OverloadAlarmLevel.RED
    print("  ✓ 110% 超载比例: 红色告警")

    assert determine_alarm_level(1.25) == OverloadAlarmLevel.RED
    print("  ✓ 125% 超载比例: 红色告警")


def test_weight_report_processing_and_alarms():
    print("\n=== 测试4: 称重数据上报与超载告警处理 ===")
    _setup_test_env()

    cranes_current_status["TEST-CRANE-001"] = CraneStatus(
        crane_id="TEST-CRANE-001",
        rotation_angle=90.0,
        trolley_position=30.0,
        hook_height=20.0,
        timestamp=time.time(),
    )

    allowed_at_30m = calculate_allowed_load("TEST-CRANE-001", 30.0)
    print(f"  变幅30m处允许载荷: {allowed_at_30m} 吨")

    base_ts = time.time()
    report_normal = WeightSensorReport(
        crane_id="TEST-CRANE-001",
        weight=allowed_at_30m * 0.5,
        sensor_timestamp=base_ts,
    )
    result = process_weight_report(report_normal)
    assert result["recorded"] is True
    assert result["alarm_triggered"] is False
    assert result["crane_locked"] is False
    print(f"  ✓ 正常载荷({report_normal.weight:.2f}吨, {result['overload_ratio']*100:.0f}%): 无告警")

    report_yellow = WeightSensorReport(
        crane_id="TEST-CRANE-001",
        weight=allowed_at_30m * 0.95,
        sensor_timestamp=base_ts + 2,
    )
    result = process_weight_report(report_yellow)
    assert result["alarm_triggered"] is True
    assert result["alarm_level"] == "YELLOW"
    assert result["crane_locked"] is False
    lock = cranes_lock_status["TEST-CRANE-001"]
    assert lock.is_locked is False
    print(f"  ✓ 黄色预警({report_yellow.weight:.2f}吨, {result['overload_ratio']*100:.0f}%): 告警触发, 塔吊未锁定")

    report_orange = WeightSensorReport(
        crane_id="TEST-CRANE-001",
        weight=allowed_at_30m * 1.05,
        sensor_timestamp=base_ts + 4,
    )
    result = process_weight_report(report_orange)
    assert result["alarm_triggered"] is True
    assert result["alarm_level"] == "ORANGE"
    assert result["crane_locked"] is True
    lock = cranes_lock_status["TEST-CRANE-001"]
    assert lock.is_locked is True
    print(f"  ✓ 橙色告警({report_orange.weight:.2f}吨, {result['overload_ratio']*100:.0f}%): 塔吊已锁定")

    lock.is_locked = False
    lock.locked_reason = None
    lock.locked_at = None

    report_red = WeightSensorReport(
        crane_id="TEST-CRANE-001",
        weight=allowed_at_30m * 1.15,
        sensor_timestamp=base_ts + 6,
    )
    result = process_weight_report(report_red)
    assert result["alarm_triggered"] is True
    assert result["alarm_level"] == "RED"
    assert result["crane_locked"] is True
    assert result["emergency_notification"] is True
    print(f"  ✓ 红色告警({report_red.weight:.2f}吨, {result['overload_ratio']*100:.0f}%): 塔吊锁定+紧急通知推送")

    history = get_weight_history("TEST-CRANE-001")
    assert len(history) == 4
    print(f"  ✓ 称重历史记录数: {len(history)} 条")

    alarms = get_overload_alarm_history("TEST-CRANE-001")
    assert len(alarms) == 3
    yellow_alarms = [a for a in alarms if a.alarm_level == OverloadAlarmLevel.YELLOW]
    orange_alarms = [a for a in alarms if a.alarm_level == OverloadAlarmLevel.ORANGE]
    red_alarms = [a for a in alarms if a.alarm_level == OverloadAlarmLevel.RED]
    assert len(yellow_alarms) == 1
    assert len(orange_alarms) == 1
    assert len(red_alarms) == 1
    print(f"  ✓ 超载告警历史: 黄={len(yellow_alarms)}, 橙={len(orange_alarms)}, 红={len(red_alarms)}")


def test_weight_history_ring_buffer():
    print("\n=== 测试5: 称重数据内存缓存环形队列(300条上限) ===")
    _setup_test_env()

    cranes_current_status["TEST-CRANE-001"] = CraneStatus(
        crane_id="TEST-CRANE-001",
        rotation_angle=0.0,
        trolley_position=20.0,
        hook_height=15.0,
        timestamp=time.time(),
    )

    base_ts = time.time()
    total_to_send = 350
    for i in range(total_to_send):
        report = WeightSensorReport(
            crane_id="TEST-CRANE-001",
            weight=1.0 + i * 0.01,
            sensor_timestamp=base_ts + i * 2,
        )
        process_weight_report(report)

    records = get_weight_history("TEST-CRANE-001")
    assert len(records) == MAX_WEIGHT_RECORDS_PER_CRANE, f"期望300条, 实际{len(records)}条"
    print(f"  ✓ 上报 {total_to_send} 条后, 缓存保留最新 {len(records)} 条 (环形队列淘汰最旧)")

    first_record = records[0]
    last_record = records[-1]
    expected_oldest_idx = total_to_send - MAX_WEIGHT_RECORDS_PER_CRANE
    expected_oldest_weight = 1.0 + expected_oldest_idx * 0.01
    expected_newest_weight = 1.0 + (total_to_send - 1) * 0.01
    assert abs(first_record.weight - expected_oldest_weight) < 0.001
    assert abs(last_record.weight - expected_newest_weight) < 0.001
    print(f"  ✓ 首条重量: {first_record.weight:.2f} 吨 (第{expected_oldest_idx}条), 末条重量: {last_record.weight:.2f} 吨 (第{total_to_send-1}条)")


def test_envelope_hot_update():
    print("\n=== 测试6: 力矩包络曲线热更新 ===")
    _setup_test_env()

    allowed_before = calculate_allowed_load("TEST-CRANE-001", 30.0)
    print(f"  更新前30m处允许载荷: {allowed_before} 吨")

    new_points = [
        LoadMomentEnvelopePoint(distance=5.0, max_load=12.0),
        LoadMomentEnvelopePoint(distance=15.0, max_load=10.0),
        LoadMomentEnvelopePoint(distance=30.0, max_load=7.0),
        LoadMomentEnvelopePoint(distance=45.0, max_load=4.5),
        LoadMomentEnvelopePoint(distance=60.0, max_load=3.0),
    ]
    success = set_envelope_curve("TEST-CRANE-001", new_points)
    assert success is True

    allowed_after = calculate_allowed_load("TEST-CRANE-001", 30.0)
    assert abs(allowed_after - 7.0) < 0.001
    print(f"  更新后30m处允许载荷: {allowed_after} 吨 (已生效, 无需重启)")

    assert allowed_after != allowed_before
    print("  ✓ 热更新生效成功，前后值不同")

    bad_points = new_points[:3]
    try:
        set_envelope_curve("TEST-CRANE-001", bad_points)
        assert False, "应抛出异常"
    except ValueError as e:
        assert "至少需要5个" in str(e)
        print(f"  ✓ 非法数据更新被拒绝: {e}")

    allowed_after_invalid = calculate_allowed_load("TEST-CRANE-001", 30.0)
    assert abs(allowed_after_invalid - 7.0) < 0.001
    print("  ✓ 更新失败后，原有配置保持不变")


def test_realtime_load_status():
    print("\n=== 测试7: 当前实时载荷状态查询 ===")
    _setup_test_env()

    cranes_current_status["TEST-CRANE-001"] = CraneStatus(
        crane_id="TEST-CRANE-001",
        rotation_angle=180.0,
        trolley_position=25.0,
        hook_height=25.0,
        timestamp=time.time(),
    )

    allowed_at_25m = calculate_allowed_load("TEST-CRANE-001", 25.0)

    ts = time.time()
    weight_val = allowed_at_25m * 0.92
    report = WeightSensorReport(
        crane_id="TEST-CRANE-001",
        weight=weight_val,
        sensor_timestamp=ts,
    )
    process_weight_report(report)

    status = get_realtime_load_status("TEST-CRANE-001")
    assert status is not None
    assert status.crane_id == "TEST-CRANE-001"
    assert abs(status.latest_weight - weight_val) < 0.001
    assert status.current_trolley_position == 25.0
    assert abs(status.allowed_load - allowed_at_25m) < 0.001
    assert status.alarm_level == OverloadAlarmLevel.YELLOW
    assert status.is_locked is False
    print(f"  ✓ 实时状态: 重量={status.latest_weight:.2f}t, 允许={status.allowed_load:.2f}t, "
          f"比例={status.overload_ratio*100:.0f}%, 告警={status.alarm_level.value}, 锁定={status.is_locked}")


def run_all_tests():
    print("=" * 60)
    print("塔吊称重与超载监控模块 - 单元测试")
    print("=" * 60)

    try:
        test_validate_envelope_points()
        test_calculate_allowed_load_linear_interpolation()
        test_determine_alarm_level()
        test_weight_report_processing_and_alarms()
        test_weight_history_ring_buffer()
        test_envelope_hot_update()
        test_realtime_load_status()

        print("\n" + "=" * 60)
        print("🎉 所有测试通过！")
        print("=" * 60)
        return True
    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False
    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
