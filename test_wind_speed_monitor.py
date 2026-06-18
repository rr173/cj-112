import time

from collision import cranes_config, cranes_lock_status, alarm_history
from models import TowerCraneConfig, LockStatus, WindSpeedReport
from wind_speed_monitor import (
    init_wind_speed_monitor_module,
    process_wind_speed_report,
    get_wind_speed_status,
    get_wind_alarm_history,
    get_wind_recovery_history,
    manual_release_wind_shutdown,
    update_wind_config,
    is_crane_wind_shutdown,
    get_wind_stats,
)


def run_tests():
    print("=" * 60)
    print("风速监测模块功能测试")
    print("=" * 60)

    if 'TEST-CRANE-001' not in cranes_config:
        cranes_config['TEST-CRANE-001'] = TowerCraneConfig(
            crane_id='TEST-CRANE-001',
            name='测试塔吊1号',
            tower_x=0.0,
            tower_y=0.0,
            tower_z=50.0,
            arm_length=60.0,
            max_load=10.0,
        )
        cranes_lock_status['TEST-CRANE-001'] = LockStatus(
            crane_id='TEST-CRANE-001', is_locked=False
        )

    init_wind_speed_monitor_module()
    print('[测试1] 模块初始化 ✓')

    print()
    print('[测试2] 正常风速上报')
    report = WindSpeedReport(
        crane_id='TEST-CRANE-001',
        wind_speed=10.0,
        sensor_timestamp=time.time(),
    )
    result = process_wind_speed_report(report)
    assert result['recorded'] == True
    assert result['alarm_triggered'] == False
    print('  ✓ 正常风速上报测试通过')

    print()
    print('[测试3] 风速预警（60秒平均超过15m/s）')
    for i in range(15):
        report = WindSpeedReport(
            crane_id='TEST-CRANE-001',
            wind_speed=16.0,
            sensor_timestamp=time.time() - (14 - i) * 5,
        )
        process_wind_speed_report(report)
    status = get_wind_speed_status('TEST-CRANE-001')
    print(f'  60秒平均风速: {status.avg_wind_speed_60s:.1f} m/s')
    alarms = get_wind_alarm_history(crane_id='TEST-CRANE-001')
    warning_alarms = [a for a in alarms if a.alarm_level.value == 'WARNING']
    print(f'  预警告警数量: {len(warning_alarms)}')
    assert len(warning_alarms) >= 1
    print('  ✓ 风速预警测试通过')

    print()
    print('[测试4] 风速停机（瞬时超过20m/s）')
    report = WindSpeedReport(
        crane_id='TEST-CRANE-001',
        wind_speed=22.0,
        sensor_timestamp=time.time(),
    )
    result = process_wind_speed_report(report)
    assert result['is_wind_shutdown'] == True
    assert is_crane_wind_shutdown('TEST-CRANE-001') == True
    assert cranes_lock_status['TEST-CRANE-001'].is_locked == True
    alarms = get_wind_alarm_history(crane_id='TEST-CRANE-001')
    shutdown_alarms = [a for a in alarms if a.alarm_level.value == 'SHUTDOWN']
    print(f'  停机告警数量: {len(shutdown_alarms)}')
    assert len(shutdown_alarms) >= 1
    print('  ✓ 风速停机测试通过')

    print()
    print('[测试5] 停机期间风速数据记录')
    report = WindSpeedReport(
        crane_id='TEST-CRANE-001',
        wind_speed=15.0,
        sensor_timestamp=time.time(),
    )
    result = process_wind_speed_report(report)
    assert result['recorded'] == True
    assert result['is_wind_shutdown'] == True
    print('  ✓ 停机期间数据记录测试通过')

    print()
    print('[测试6] 自动恢复（连续10条低于16m/s）')
    recovery_triggered = False
    for i in range(10):
        report = WindSpeedReport(
            crane_id='TEST-CRANE-001',
            wind_speed=14.0,
            sensor_timestamp=time.time(),
        )
        result = process_wind_speed_report(report)
        if result.get('recovery_triggered'):
            recovery_triggered = True
            print(f'  第{i+1}条数据后触发自动恢复')
            break
    assert recovery_triggered == True
    assert is_crane_wind_shutdown('TEST-CRANE-001') == False
    assert cranes_lock_status['TEST-CRANE-001'].is_locked == False
    recoveries = get_wind_recovery_history(crane_id='TEST-CRANE-001')
    assert len(recoveries) >= 1
    recovery = recoveries[-1]
    print(f'  恢复方式: {recovery.recovery_method}')
    print(f'  停机期间最大风速: {recovery.max_wind_speed_during_shutdown:.1f} m/s')
    print(f'  恢复前平均风速: {recovery.avg_wind_speed_before_recovery:.1f} m/s')
    print(f'  停机时长: {recovery.shutdown_duration_seconds:.0f} 秒')
    assert recovery.recovery_method == 'AUTO'
    print('  ✓ 自动恢复测试通过')

    print()
    print('[测试7] 手动解除停机')
    report = WindSpeedReport(
        crane_id='TEST-CRANE-001',
        wind_speed=25.0,
        sensor_timestamp=time.time(),
    )
    result = process_wind_speed_report(report)
    assert result['is_wind_shutdown'] == True
    print('  再次触发停机成功')

    result = manual_release_wind_shutdown('TEST-CRANE-001')
    assert result['success'] == True
    assert is_crane_wind_shutdown('TEST-CRANE-001') == False
    assert cranes_lock_status['TEST-CRANE-001'].is_locked == False
    recoveries = get_wind_recovery_history(crane_id='TEST-CRANE-001')
    assert len(recoveries) >= 2
    assert recoveries[-1].recovery_method == 'MANUAL'
    print(f'  最后一次恢复方式: {recoveries[-1].recovery_method}')
    print('  ✓ 手动解除测试通过')

    print()
    print('[测试8] 配置热更新')
    result = update_wind_config(
        crane_id='TEST-CRANE-001',
        shutdown_threshold=25.0,
        warning_threshold=18.0,
    )
    assert result['success'] == True
    status = get_wind_speed_status('TEST-CRANE-001')
    assert status.current_config.shutdown_threshold == 25.0
    assert status.current_config.warning_threshold == 18.0
    print(f'  更新后停机阈值: {status.current_config.shutdown_threshold}')
    print(f'  更新后预警阈值: {status.current_config.warning_threshold}')
    print('  ✓ 配置热更新测试通过')

    print()
    print('[测试9] 告警写入通用告警历史')
    wind_alarms_in_history = [
        a for a in alarm_history
        if a.alarm_type.value in ('WIND_SPEED_WARNING', 'WIND_SPEED_SHUTDOWN')
        and a.crane_a_id == 'TEST-CRANE-001'
    ]
    print(f'  alarm_history中风速告警数量: {len(wind_alarms_in_history)}')
    assert len(wind_alarms_in_history) >= 3
    print('  ✓ 告警写入通用告警历史测试通过')

    print()
    print('[测试10] 统计信息')
    stats = get_wind_stats()
    print(f'  总告警数: {stats["total_alarms"]}')
    print(f'  预警数: {stats["warning_count"]}')
    print(f'  停机数: {stats["shutdown_count"]}')
    print(f'  总恢复数: {stats["total_recoveries"]}')
    print(f'  自动恢复数: {stats["auto_recoveries"]}')
    print(f'  手动恢复数: {stats["manual_recoveries"]}')
    print(f'  当前停机数: {stats["current_shutdowns"]}')
    print('  ✓ 统计信息测试通过')

    print()
    print('=' * 60)
    print('所有测试通过！✓')
    print('=' * 60)

    if 'TEST-CRANE-001' in cranes_config:
        del cranes_config['TEST-CRANE-001']
    if 'TEST-CRANE-001' in cranes_lock_status:
        del cranes_lock_status['TEST-CRANE-001']


if __name__ == '__main__':
    run_tests()
