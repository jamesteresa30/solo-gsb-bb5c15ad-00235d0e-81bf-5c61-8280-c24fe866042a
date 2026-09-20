# 实验室样本冻融追踪 API

基于 **FastAPI + SQLite + Docker** 的服务，用于追踪实验室样本（疫苗、生物制剂、细胞株等）在冷链过程中的冻融循环：

- 客户端按样本提交**带事件时间**的温度读数；
- 服务端按 `event_time` 时间顺序**重排迟到数据**后重算，而不是按入库顺序处理；
- 使用可配置的**冻结/解冻阈值 + 滞回（Schmitt 触发器）**规则识别完整冻融循环，避免临界温度抖动造成的误判；
- 重复事件通过客户端 `event_id`（或“同时刻同温度”签名）**幂等去重**；
- 暴露样本**当前状态、完整循环次数/明细、温度越限与循环超限告警**查询与确认接口；
- 支持**累计温度暴露剂量（°C·分钟）**：按相邻读数线性插值积分越限幅度，
  超低温/超高温双向预算首次跨越时产生稳定去重的 `exposure_budget_exceeded` 告警，
  间隔过大的数据缺口跳过积分并单独统计；
- 容器启动即自动初始化数据库（建表、索引、旧版本库兼容迁移）。

## 目录结构

```
app/
  main.py        # FastAPI 路由与启动初始化
  engine.py      # 纯函数冻融状态机（循环识别 + 告警推导），不依赖 Web/DB
  database.py    # SQLite 连接与幂等建表脚本
  schemas.py     # Pydantic 请求/响应模型
  services.py    # 业务层：幂等写入、时间重排重算、告警确认
tests/           # pytest：引擎单元测试 + API 集成测试
examples/        # requests.http / demo.sh 示例请求
Dockerfile
docker-compose.yml
```

## 快速开始（Docker）

一键启动（自动构建镜像、初始化/迁移数据库）：

```bash
docker compose up --build
```

启动后服务监听 **http://localhost:8000** ：

- Swagger 文档：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc
- 健康检查：http://localhost:8000/health

SQLite 数据文件持久化在 named volume `freeze-thaw-data`（容器内 `/data/freeze_thaw.db`），
首次启动自动建库；升级到含暴露剂量字段的新版本时会对旧库执行幂等 `ALTER TABLE` 兼容迁移，历史数据保留。

## 本地开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 启动（默认数据文件 ./data/freeze_thaw.db，可用 DB_PATH 覆盖）
uvicorn app.main:app --reload

# 运行测试
pytest -q
```

## 冻融识别规则（滞回）

为每个样本配置两个阈值：`freeze_threshold_c < thaw_threshold_c`（示例：-20°C / 0°C）。

| 当前状态 | 条件 | 迁移 |
 | --- | --- | --- |
| `thawed`（已解冻） | 温度 **≤ freeze_threshold_c** | → `frozen`，记录冻结起点 |
| `frozen`（冻结中） | 温度 **≥ thaw_threshold_c** | → `thawed`，**闭环一次完整冻融循环** |
| `frozen` | 温度处于两阈值之间（如 -10°C） | 保持 `frozen`（滞回，不翻转） |
| `thawed` | 温度处于两阈值之间 | 保持 `thawed` |

每个完整循环记录：冻结起点时间/温度、解冻时间/温度、循环段内最低/最高温度。
**只冻结未解冻不算循环**；当前仍处于冻结段时，状态接口会返回冻结起点与段内极值。

### 告警规则

1. **`temperature_excursion`（critical）**：读数超出样本允许区间
   `[min_allowed_c, max_allowed_c]`。连续越限只告警一次（去抖），回到区间后再次越限才再次告警。
2. **`cycle_limit_exceeded`（warning）**：完整循环次数超过 `max_freeze_thaw_cycles`，每超出一次循环告警一次。
3. **`exposure_budget_exceeded`（warning）**：累计温度暴露剂量首次达到预算（低温/高温各最多一条，
   字段 `exposure_side = low/high`）。剂量、缺口与该告警同样由重算驱动，迟到数据或配置
   收紧/放宽后可双向更新：不再成立的告警会被撤销，重新成立时按稳定去重键恢复（含确认状态）。

告警支持单条确认、按样本批量确认；重算会保留确认状态（按稳定去重键 upsert）。

## 累计温度暴露剂量

对按 `event_time` 排序后的**相邻读数做线性插值**，把温度曲线在允许区间之外的区段切分积分，
单位 **°C·分钟（超限幅度 × 分钟）**：

- **超低温剂量** `low_exposure_c_min` = 低于 `min_allowed_c` 的幅度对分钟的积分；
- **超高温剂量** `high_exposure_c_min` = 高于 `max_allowed_c` 的幅度对分钟的积分；
- 两读数间隔超过 `max_integration_gap_minutes` 时该段**不积分**（视为数据缺口），
  间隔时长累计到 `exposure_gap_minutes`；未配置该参数则不设上限；
- 阈值穿越点通过线性插值确定，越限段为三角形/梯形面积，无需读数恰好落在阈值上；
- 预算 `low_exposure_budget_c_min` / `high_exposure_budget_c_min`（°C·分钟）被首次跨越时
  产生告警，跨越时间按段内剂量线性插值估计。

状态接口 `GET /samples/{id}/state` 同时返回双向剂量、缺口时长与两侧预算使用率
（`low/high_exposure_budget_used_ratio`，未配置预算侧为 `null`）。

## 迟到数据与重算

每次成功写入新读数后，服务都会：

1. 取出该样本**全部**读数，按 `(event_time, id)` 升序重排（迟到数据自然回到正确时间位置）；
2. 用 `engine.derive` 纯函数重放状态机，重建完整循环、**累计双向暴露剂量/缺口**与告警；
3. 刷新样本当前状态缓存（含剂量缓存）。

阈值、允许区间、循环上限或剂量预算/积分间隔发生 PATCH 变更时也会走同一重算流程，
因此剂量与告警（含确认状态）始终与最新读数和配置保持一致，更新完全可逆。

也可调用 `POST /samples/{id}/recompute` 手动触发兜底重算。

## 幂等去重

- 提供 `event_id` 时：`(sample_id, event_id)` 唯一，重复提交直接跳过；
- 不提供 `event_id` 时：`(sample_id, event_time, temperature_c)` 相同视为重复；
- 同一请求批次内的重复同样会被识别。

批量提交响应返回 `accepted`（新写入）与 `duplicates`（跳过）计数。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/samples` | 创建样本（阈值、允许区间、循环上限可配置） |
| GET | `/samples` | 样本列表（含状态/循环数摘要） |
| GET/PATCH/DELETE | `/samples/{id}` | 样本详情 / 修改配置（自动重算） / 删除 |
| POST | `/samples/{id}/readings` | 批量提交读数（幂等，提交后自动重算，返回新告警） |
| GET | `/samples/{id}/readings` | 读数列表（按事件时间排序） |
| GET | `/samples/{id}/state` | **当前状态**、循环次数、双向剂量/缺口/预算使用率、未确认告警数、最近读数 |
| GET | `/samples/{id}/cycles` | **完整冻融循环明细** |
| POST | `/samples/{id}/recompute` | 手动重算 |
| GET | `/alerts` | **告警查询**（可按样本、确认状态过滤，分页） |
| GET/POST | `/alerts/{id}` `/alerts/{id}/ack` | 告警详情 / 确认（或取消确认） |
| POST | `/samples/{id}/alerts/ack` | 批量确认样本下所有告警 |

## 示例请求（curl）

```bash
# 1. 创建样本
curl -X POST http://localhost:8000/samples \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "sample-demo-001",
    "name": "新冠灭活疫苗 批次 A-2026-09",
    "freeze_threshold_c": -20,
    "thaw_threshold_c": 0,
    "min_allowed_c": -80,
    "max_allowed_c": 8,
    "max_freeze_thaw_cycles": 2,
    "low_exposure_budget_c_min": 2000,
    "high_exposure_budget_c_min": 500,
    "max_integration_gap_minutes": 180
  }'

# 2. 提交读数（完成一次冻融）
curl -X POST http://localhost:8000/samples/sample-demo-001/readings \
  -H 'Content-Type: application/json' \
  -d '{"readings": [
    {"event_id": "evt-1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4.0},
    {"event_id": "evt-2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -22.0},
    {"event_id": "evt-3", "event_time": "2026-09-20T10:30:00Z", "temperature_c": 2.0}
  ]}'

# 3. 迟到数据（09:00/09:30，服务端自动插入时间轴中间重排）
curl -X POST http://localhost:8000/samples/sample-demo-001/readings \
  -H 'Content-Type: application/json' \
  -d '{"readings": [
    {"event_id": "late-1", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -25.0},
    {"event_id": "late-2", "event_time": "2026-09-20T09:30:00Z", "temperature_c": 3.0}
  ]}'

# 4. 查询当前状态与循环
curl http://localhost:8000/samples/sample-demo-001/state
curl http://localhost:8000/samples/sample-demo-001/cycles

# 5. 越限告警 / 未确认告警 / 确认
curl 'http://localhost:8000/alerts?sample_id=sample-demo-001'
curl 'http://localhost:8000/alerts?acknowledged=false'
curl -X POST http://localhost:8000/samples/sample-demo-001/alerts/ack \
  -H 'Content-Type: application/json' -d '{"acknowledged": true}'
```

更多可直接执行的示例：

- `examples/requests.http`（VS Code REST Client / IntelliJ HTTP Client）
- `examples/demo.sh`（curl 端到端脚本：正常数据 → 迟到重排 → 幂等去重 → 越限/超限告警）

```bash
chmod +x examples/demo.sh && ./examples/demo.sh
```

## 时间处理

- 输入接受任意 ISO 8601 时间；带时区的统一换算为 UTC，**无时区时间按 UTC 处理**。
- 内部以 UTC ISO 8601 字符串存储，字典序即时间序，迟到数据只需插入即可参与正确重算。

## 配置项

| 环境变量 | 默认值 | 说明 |
 | --- | --- | --- |
| `DB_PATH` | 本地 `data/freeze_thaw.db`；镜像内 `/data/freeze_thaw.db` | SQLite 文件路径，启动自动建库 |

样本级配置（创建时指定，可 PATCH 修改）：`freeze_threshold_c`、`thaw_threshold_c`、
`min_allowed_c`、`max_allowed_c`、`max_freeze_thaw_cycles`，以及暴露剂量相关的
`low_exposure_budget_c_min`、`high_exposure_budget_c_min`（°C·分钟预算，正数）、
`max_integration_gap_minutes`（相邻读数最大可积分间隔，分钟，正数）。

## 测试

```bash
pytest -q
```

覆盖内容：阈值边界与滞回、死区防抖、不完整冻结不计循环、温度越限去抖告警、
循环超限告警、暴露剂量线性插值/阈值穿越切分/缺口跳过/双向预算首次跨越与稳定去重、
迟到数据重排后剂量与告警可逆更新、预算 PATCH 触发重算、旧版数据库 ALTER 兼容迁移、
`event_id`/签名双重幂等、告警确认在重算后保留、404 处理。
