# 塔吊防碰撞联锁服务

建筑工地多塔吊防碰撞实时监测系统，实时接收塔吊状态，检测碰撞风险，触发锁定并记录告警。

## 功能特性

- 预置3台塔吊基础参数（塔身坐标、臂长、最大起重量、回转范围限制）
- 各塔吊对之间默认5米安全间距阈值
- REST接口每秒上报状态（回转角度、变幅小车位置、吊钩高度）
- 极坐标转笛卡尔坐标 + 三维欧氏距离碰撞检测
- 吊钩摆幅按吊绳长度10%估算，叠加到安全距离判定
- 触发告警后相关塔吊自动锁定，锁定期间拒绝状态上报
- 提供人工解除锁定接口
- 查询实时状态、告警历史（含完整坐标快照）、锁定状态

## 快速启动

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动服务

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

服务启动后访问：
- API文档: http://localhost:8000/docs
- 健康检查: http://localhost:8000/health

## 预置塔吊配置

| 塔吊ID | 塔身坐标(X,Y) | 塔身顶部高度 | 臂长 | 最大起重量 | 回转范围 |
|--------|---------------|--------------|------|------------|----------|
| CRANE-001 | (0.0, 0.0) | 50m | 60m | 10吨 | 0°-360° |
| CRANE-002 | (40.0, 30.0) | 55m | 55m | 8吨 | 0°-360° |
| CRANE-003 | (-35.0, 45.0) | 48m | 50m | 12吨 | 45°-315° |

## API 接口列表

### 状态上报
```
POST /api/crane/status
```
请求体：
```json
{
  "crane_id": "CRANE-001",
  "rotation_angle": 45.0,
  "trolley_position": 30.0,
  "hook_height": 20.0
}
```
- 返回423表示塔吊已锁定

### 解除锁定
```
POST /api/crane/{crane_id}/unlock
```

### 查询单台塔吊实时状态
```
GET /api/crane/{crane_id}/status
```
返回：配置参数、当前状态、锁定状态、臂端/吊钩三维坐标、吊钩摆幅

### 查询所有塔吊
```
GET /api/cranes
```

### 查询告警历史
```
GET /api/alarms?crane_id=CRANE-001&limit=100
```
包含触发时两台塔吊的完整坐标快照

### 查询所有锁定状态
```
GET /api/locks
```

### 查询安全阈值配置
```
GET /api/thresholds
```

## 碰撞检测算法说明

1. **坐标转换**: 极坐标(回转角+变幅距离) → 笛卡尔坐标(X,Y,Z)
2. **距离计算**: 分别计算吊臂端点之间、吊钩位置之间的三维欧氏距离
3. **吊钩摆幅**: 吊绳长度(塔身顶部高度 - 吊钩高度) × 10% 作为水平偏移量
4. **安全判定**: 最小距离 < 基础安全阈值(5m) + 两台塔吊摆幅之和 → 触发告警并锁定

## 测试示例

上报安全状态（距离足够远）：
```bash
curl -X POST http://localhost:8000/api/crane/status \
  -H "Content-Type: application/json" \
  -d '{"crane_id":"CRANE-001","rotation_angle":0,"trolley_position":10,"hook_height":30}'

curl -X POST http://localhost:8000/api/crane/status \
  -H "Content-Type: application/json" \
  -d '{"crane_id":"CRANE-002","rotation_angle":180,"trolley_position":10,"hook_height":30}'
```

触发碰撞（吊臂靠近对方塔身）：
```bash
curl -X POST http://localhost:8000/api/crane/status \
  -H "Content-Type: application/json" \
  -d '{"crane_id":"CRANE-001","rotation_angle":36.87,"trolley_position":50,"hook_height":30}'

curl -X POST http://localhost:8000/api/crane/status \
  -H "Content-Type: application/json" \
  -d '{"crane_id":"CRANE-002","rotation_angle":216.87,"trolley_position":40,"hook_height":30}'
```

解除锁定：
```bash
curl -X POST http://localhost:8000/api/crane/CRANE-001/unlock
curl -X POST http://localhost:8000/api/crane/CRANE-002/unlock
```
