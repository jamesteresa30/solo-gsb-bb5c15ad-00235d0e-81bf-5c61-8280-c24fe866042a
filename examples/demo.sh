#!/usr/bin/env bash
# 端到端示例：创建样本 -> 正常冻融 -> 迟到数据重排 -> 幂等去重 -> 越限/超限告警 -> 查询
# 用法: ./examples/demo.sh （需先在 localhost:8000 启动服务）
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
SID="sample-demo-001"

req() { curl -sS -X "$@"; }

echo "== 健康检查 =="
curl -sS "$BASE/health"; echo

echo "== 创建样本 =="
curl -sS -X POST "$BASE/samples" -H 'Content-Type: application/json' -d "{
  \"id\": \"$SID\",
  \"name\": \"新冠灭活疫苗 批次 A-2026-09\",
  \"freeze_threshold_c\": -20,
  \"thaw_threshold_c\": 0,
  \"min_allowed_c\": -80,
  \"max_allowed_c\": 8,
  \"max_freeze_thaw_cycles\": 2
}"; echo

echo "== 第一批读数（完成第 1 次冻融）=="
curl -sS -X POST "$BASE/samples/$SID/readings" -H 'Content-Type: application/json' -d '{
  "readings": [
    {"event_id": "evt-001", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4.0},
    {"event_id": "evt-002", "event_time": "2026-09-20T08:15:00Z", "temperature_c": -5.0},
    {"event_id": "evt-003", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -22.0},
    {"event_id": "evt-004", "event_time": "2026-09-20T10:00:00Z", "temperature_c": -10.0},
    {"event_id": "evt-005", "event_time": "2026-09-20T10:30:00Z", "temperature_c": 2.0}
  ]
}'; echo

echo "== 迟到数据（09:00/09:30，重排后与 10:00/10:30 共同多出一次循环）=="
curl -sS -X POST "$BASE/samples/$SID/readings" -H 'Content-Type: application/json' -d '{
  "readings": [
    {"event_id": "evt-late-1", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 3.0},
    {"event_id": "evt-late-2", "event_time": "2026-09-20T09:30:00Z", "temperature_c": -25.0}
  ]
}'; echo

echo "== 重复 event_id（duplicates 应为 1，accepted 为 0）=="
curl -sS -X POST "$BASE/samples/$SID/readings" -H 'Content-Type: application/json' -d '{
  "readings": [
    {"event_id": "evt-003", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -22.0}
  ]
}'; echo

echo "== 越限 + 第 3 次循环 =="
curl -sS -X POST "$BASE/samples/$SID/readings" -H 'Content-Type: application/json' -d '{
  "readings": [
    {"event_id": "evt-006", "event_time": "2026-09-20T11:00:00Z", "temperature_c": -85.5},
    {"event_id": "evt-007", "event_time": "2026-09-20T11:30:00Z", "temperature_c": 5.0}
  ]
}'; echo

echo "== 当前状态 =="
curl -sS "$BASE/samples/$SID/state"; echo

echo "== 循环列表 =="
curl -sS "$BASE/samples/$SID/cycles"; echo

echo "== 全部告警 =="
curl -sS "$BASE/alerts?sample_id=$SID"; echo
