# 实验室样本冻融追踪 API

基于 **FastAPI + SQLite + Docker** 的服务，用于追踪实验室样本（疫苗、生物制剂、细胞株等）在冷链过程中的冻融循环：

- 客户端按样本提交**带事件时间**的温度读数；
- 服务端按 `event_time` 时间顺序**重排迟到数据**后重算，而不是按入库顺序处理；
- 使用可配置的**冻结/解冻阈值 + 滞回（Schmitt 触发器）**规则识别完整冻融循环，避免临界温度抖动造成的误判；
- 重复事件通过客户端 `event_id`（或“同时刻同温度”签名）**幂等去重**；
- 暴露样本**当前状态、完整循环次数/明细、温度越限与循环超限告警**查询与确认接口；
- 按相邻读数线性插值累计**双向温度暴露剂量（超限幅度 × 分钟）**，支持低温/高温剂量预算、
  最大可积分间隔（缺口跳过）与预算超限告警；
- 容器启动即自动初始化数据库（建表、索引），旧版本数据库自动兼容迁移。

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

```bash
docker compose up --build
```

服务监听 http://localhost:8000 ：

- Swagger 文档：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc
- 健康检查：http://localhost:8000/health

SQLite 数据文件持久化在 named volume `freeze-thaw-data`（容器内 `/data/freeze_thaw.db`），首次启动自动建库。

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

告警支持单条确认、按样本批量确认；重算会保留确认状态（按稳定去重键 upsert）。

## 累计温度暴露剂量

在冻融状态机之外，服务还会按读数时间轴累计**温度暴露剂量**（单位 °C·min，即
“超出允许区间的幅度 × 持续分钟数”）：

1. 对按 `event_time` 排序后的**相邻读数做线性插值**，把曲线越过 `min_allowed_c`
   或 `max_allowed_c` 的区段在跨越点切分（线性线段在一个区间内至多跨越一次）；
2. 低温方向累计 `min_allowed_c - 温度` 的积分，高温方向累计
   `温度 - max_allowed_c` 的积分（梯形面积，界内幅度计 0）；
3. 相邻读数间隔超过 `max_integration_interval_minutes` 时，该区间**整体跳过不积分**，
   间隔时长计入 `exposure_gap_minutes`（缺口时长）；恰好等于上限的间隔仍参与积分；
4. 任一方向累计剂量**首次达到**对应预算（`cold_dose_budget_c_min` /
   `heat_dose_budget_c_min`）时，产生一条 `exposure_budget_exceeded`（critical）告警，
   告警事件时间为插值求出的精确跨越时刻；每个方向至多一条，去重键为
   `sample_id:exposure_budget:cold|heat`。

剂量、缺口与告警全部由重算流程派生：**迟到读数补录、阈值 / 预算 / 积分间隔 PATCH
修改后都会整体重算**——预算放宽后超限告警会消失（确认状态随去重键保留），预算收紧后
会重新出现，保证剂量与告警可逆更新。

> 配置剂量预算时必须同时提供对应的允许区间边界（冷预算需 `min_allowed_c`，
> 热预算需 `max_allowed_c`）。

## 迟到数据与重算

每次成功写入新读数后，服务都会：

1. 取出该样本**全部**读数，按 `(event_time, id)` 升序重排（迟到数据自然回到正确时间位置）；
2. 用 `engine.derive` 纯函数重放状态机，重建完整循环、暴露剂量与告警；
3. 刷新样本当前状态缓存。

PATCH 样本的阈值、允许区间、循环上限、剂量预算或最大可积分间隔后，也会走同一条重算
流程，因此剂量与预算告警对“迟到数据”和“配置变更”都保持可逆、可重复的更新语义。

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
| GET | `/samples/{id}/state` | **当前状态**、循环次数、双向暴露剂量/缺口/预算使用率、未确认告警数、最近读数 |
| GET | `/samples/{id}/cycles` | **完整冻融循环明细** |
| POST | `/samples/{id}/recompute` | 手动重算 |
| GET | `/alerts` | **告警查询**（可按样本、确认状态过滤，分页） |
| GET/POST | `/alerts/{id}` `/alerts/{id}/ack` | 告警详情 / 确认（或取消确认） |
| POST | `/samples/{id}/alerts/ack` | 批量确认样本下所有告警 |

## 示例请求（curl）

```bash
# 1. 创建样本（含低温/高温剂量预算与 120 分钟最大可积分间隔）
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
    "cold_dose_budget_c_min": 50,
    "heat_dose_budget_c_min": 200,
    "max_integration_interval_minutes": 120
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

# 4. 查询当前状态、暴露剂量与循环
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
`min_allowed_c`、`max_allowed_c`、`max_freeze_thaw_cycles`、
`cold_dose_budget_c_min`、`heat_dose_budget_c_min`、
`max_integration_interval_minutes`。

暴露剂量相关字段：

| 字段 | 单位 | 说明 |
| --- | --- | --- |
| `cold_dose_budget_c_min` | °C·min | 低温累计剂量预算，首次跨越产生 `exposure_budget_exceeded`（需同时配置 `min_allowed_c`） |
| `heat_dose_budget_c_min` | °C·min | 高温累计剂量预算（需同时配置 `max_allowed_c`） |
| `max_integration_interval_minutes` | 分钟 | 相邻读数最大可积分间隔，超过则跳过该区间并计入缺口 |
| `cold_dose_c_min` / `heat_dose_c_min` | °C·min | 状态接口返回的双向累计剂量（派生值） |
| `exposure_gap_minutes` | 分钟 | 因间隔超过上限而跳过的缺口总时长（派生值） |
| `cold_budget_usage` / `heat_budget_usage` | 比值 | 预算使用率 = 剂量 / 预算，`>= 1` 表示已超限；未配置预算为 `null` |

### 数据库兼容迁移

容器启动（或本地 `init_db`）时会先幂等建表，再检测旧库缺失的列并以 `ALTER TABLE`
补齐（暴露剂量配置/缓存列、告警方向与剂量列）。旧版本的 SQLite 数据文件无需手工处理
即可平滑升级，历史样本与读数保留，首次重算后剂量缓存自动填充。

## 测试

```bash
pytest -q
```

覆盖内容：阈值边界与滞回、死区防抖、不完整冻结不计循环、温度越限去抖告警、
循环超限告警、迟到数据重排、`event_id`/签名双重幂等、告警确认在重算后保留、404 处理，
以及暴露剂量的线性插值切分、双向累计、缺口跳过、预算跨越插值时刻告警、稳定去重与
配置变更可逆重算、旧版数据库 ALTER TABLE 迁移。
