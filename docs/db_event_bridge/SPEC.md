# SPEC: DB Row Trigger Event Bridge — PostgreSQL LISTEN/NOTIFY 도입

> **상태**: DRAFT (초안)
> **작성**: 2026-05-15
> **대상**: backend/main_service
> **선행 PR**: #26 (AI /predict 옵션 B 정합), #27 (mock+test 정합)
> **본 SPEC 구현은 별도 PR 단위 작업** (예상: 1~2주)

---

## 1. 배경

현재 backend 의 task lifecycle (equip/trans/pp/insp 4종 task) 자동 진행은 **process 내부 EventBridge (in-memory pub/sub)** 로만 동작합니다.

```
RPC handler → EventBridge.publish() → 같은 process subscriber → state_manager → DB UPDATE
```

이 아키텍처의 한계:
- **DB row 변경이 자동 trigger 가 되지 않음** — 외부 시스템(SQL UPDATE, 다른 service, 운영자 도구)이 DB 를 바꿔도 backend 가 반응하지 않음
- **backend 재시작 시 in-flight task 의 자연스러운 복구가 어려움** — 메모리 손실
- **시뮬레이션/테스트가 어려움** — SQL UPDATE 만으로 lifecycle 진행 불가 (이번 세션에서 검증됨)

## 2. 문제 정의

> **사용자 요구**: "DB row 변경이 backend 자동 동작을 트리거해야 한다"

즉 DB 가 진실의 원천 (Source of Truth) 이고, 어디서든 (backend RPC handler, SQL UPDATE, 다른 service, 운영 도구) row 가 바뀌면 backend lifecycle 이 그것을 보고 자동 진행해야 합니다.

## 3. 목표 / 비목표

### 목표
- **DB row 변경 → backend 자동 lifecycle 진행** (PostgreSQL `LISTEN/NOTIFY` 기반)
- **기존 EventBridge 의 in-process subscriber 호환성 유지** (점진 마이그레이션)
- **다중 backend 인스턴스 환경 지원** (모든 인스턴스가 동일 NOTIFY 채널 구독)
- **테스트/시뮬레이션 도구가 SQL UPDATE 만으로 lifecycle 진행 가능**

### 비목표
- Kafka/Redis Stream 등 외부 메시지 브로커 도입 (Outbox Pattern) — 본 SPEC 범위 외
- 모든 EventType 의 DB 화 — `INSP_IMAGE_UPLOADED`, `RFID_SCANNED` 등 일부 event 는 in-memory 채널 유지 (DB row 와 무관)
- DDL 마이그레이션 도구 변경 — 기존 alembic 흐름 유지

## 4. 설계 개요

### 4.1 컴포넌트

```
[1] DB AFTER INSERT/UPDATE trigger      (Postgres SQL)
    └─ pg_notify('lifecycle_event', json_payload)

[2] backend listener worker             (asyncpg 또는 psycopg)
    └─ LISTEN lifecycle_event;
    └─ NOTIFY payload → EventBridge.publish(adapter)

[3] EventBridge (기존)                   (in-memory pub/sub, 그대로 유지)
    └─ DB-origin event 와 in-process event 동일하게 라우팅
    └─ subscriber 는 origin 구분 없이 동작

[4] task_executor (기존)                 (subscriber, 그대로 유지)
    └─ DB-origin INSERT/UPDATE 도 자동 waiter 해제
```

### 4.2 흐름 예시 — `insp_task_txn` PROC INSERT

```
[외부 SQL or backend INSERT]
    INSERT INTO insp_task_txn (item_id, txn_stat='PROC') ...
         │
         ▼
[DB AFTER INSERT trigger]
    pg_notify('lifecycle_event', '{"event":"insp_task_txn_created","item_id":29,"txn_id":14}')
         │
         ▼
[listener worker]
    raw NOTIFY → InspTaskCreatedEvent → EventBridge.publish()
         │
         ▼
[task_executor subscriber]
    INSP task lifecycle 진입 → AI step dispatch
         │
         ▼
[state_manager.record_inspection_result]
    insp_task_txn SUCC UPDATE + ai_inference_txn INSERT + insp_stat INSERT
         │  (이 UPDATE 도 trigger 발동 → 다음 lifecycle 자동 진행)
         ▼
[다음 stage trigger]
    pg_notify('lifecycle_event', '{"event":"insp_task_txn_succeeded","item_id":29,...}')
    → ToPAWait task 등 후속 자동 dispatch
```

## 5. 데이터 모델

### 5.1 NOTIFY 채널 명명

- 단일 채널 `lifecycle_event` 사용 — payload `event` 필드로 분기
- 향후 부하 증가 시 채널 분할 가능 (`equip_event`, `trans_event` 등)

### 5.2 NOTIFY payload 스키마

```json
{
  "event": "<table>_<action>",          // ex: "insp_task_txn_created", "item_cur_stat_changed"
  "schema": "smartcast",
  "table": "insp_task_txn",
  "op": "INSERT|UPDATE",
  "row": {
    "txn_id": 14,
    "item_id": 29,
    "txn_stat": "PROC",
    "...": "..."
  },
  "old_row": { ... },                    // UPDATE 시만 (NEW vs OLD diff 용)
  "at": "2026-05-15T09:18:23.731Z"
}
```

**제약**: PostgreSQL NOTIFY payload 는 **8KB 한도**. row 가 큰 테이블 (예: `ai_inference_txn.result_json` JSONB) 은 `row.result_json: "<truncated, len=...>"` 으로 strip + listener 가 필요시 SELECT 로 재조회.

### 5.3 대상 테이블 (Phase 1)

| 테이블 | trigger | 발행 event |
|--------|---------|------------|
| `insp_task_txn` | AFTER INSERT, AFTER UPDATE OF txn_stat | `insp_task_txn_created`, `insp_task_txn_status_changed` |
| `equip_task_txn` | AFTER INSERT, AFTER UPDATE OF txn_stat | `equip_task_txn_created`, `equip_task_txn_status_changed` |
| `trans_task_txn` | AFTER INSERT, AFTER UPDATE OF txn_stat | `trans_task_txn_created`, `trans_task_txn_status_changed` |
| `pp_task_txn` | AFTER INSERT, AFTER UPDATE OF txn_stat | `pp_task_txn_created`, `pp_task_txn_status_changed` |
| `item` | AFTER UPDATE OF cur_stat, is_defective | `item_cur_stat_changed`, `item_defective_set` |
| `ord_stat` | AFTER UPDATE OF ord_stat | `ord_stat_changed` |

**제외 (Phase 1)**: `ai_inference_txn`, `insp_stat`, `log_*`, `rfid_scan_log` — 결과 영속화 row 라 후속 lifecycle 진행에 불필요.

### 5.4 Trigger SQL 예시

```sql
CREATE OR REPLACE FUNCTION smartcast.notify_lifecycle_event() RETURNS trigger AS $$
DECLARE
    event_name TEXT;
    payload JSON;
BEGIN
    event_name := TG_TABLE_NAME || '_' || lower(TG_OP);
    payload := json_build_object(
        'event', event_name,
        'schema', TG_TABLE_SCHEMA,
        'table', TG_TABLE_NAME,
        'op', TG_OP,
        'row', row_to_json(NEW),
        'old_row', CASE WHEN TG_OP = 'UPDATE' THEN row_to_json(OLD) ELSE NULL END,
        'at', to_char(now() at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
    );
    -- 8KB 제한 회피: 큰 JSONB 컬럼은 strip
    -- (구현 시 row → JSON 변환 단계에서 필터링)
    PERFORM pg_notify('lifecycle_event', payload::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_insp_task_txn_lifecycle
    AFTER INSERT OR UPDATE OF txn_stat ON smartcast.insp_task_txn
    FOR EACH ROW EXECUTE FUNCTION smartcast.notify_lifecycle_event();
```

## 6. 구현 단계

### Phase 1 — Listener Worker + Adapter 골격
- `services/core/db_event_listener.py` 신설 — asyncpg `LISTEN lifecycle_event` background task
- NOTIFY payload → `Event` 변환 어댑터 작성
- `container.py` 의 startup 에서 listener worker 실행
- 기존 `EventBridge.publish()` 인터페이스 그대로 사용 (subscriber 측 변경 없음)

### Phase 2 — Trigger 마이그레이션 (insp_task_txn 우선)
- alembic 마이그레이션: `smartcast.notify_lifecycle_event()` 함수 + `insp_task_txn` trigger 추가
- 기존 in-process `INSP_IMAGE_UPLOADED → task_executor` 흐름과 새 `insp_task_txn_created → task_executor` 흐름 병행 (dual write 방지: handler 가 idempotency_key 로 중복 차단)
- e2e 검증: SQL `INSERT INTO insp_task_txn (PROC)` 만으로 AIAdapter 자동 진행

### Phase 3 — 나머지 테이블 trigger 확장
- equip / trans / pp / item / ord_stat 순으로 trigger 추가
- 각 단계 e2e 시뮬레이션 (이번 세션의 P0~P13 SQL 시뮬레이션이 자동으로 backend 흐름 트리거하는지)

### Phase 4 — in-memory pub/sub 정리
- DB trigger 로 대체 가능한 in-process `EventBridge.publish()` 제거
- `RFID_SCANNED`, `INSP_IMAGE_UPLOADED` 등 DB 와 무관한 event 만 in-process 유지

## 7. 호환성 / 마이그레이션 전략

- **dual delivery 단계** (Phase 2): in-process 와 DB-origin 둘 다 publish — subscriber 가 idempotency_key 로 중복 차단
- **roll-back 안전성**: trigger 만 DROP 하면 즉시 in-process 동작으로 복귀
- **기존 e2e 테스트 호환**: `ai_mock_server` fixture 등 in-process flow 에 의존하는 테스트는 그대로 동작

## 8. 운영 고려사항

| 항목 | 내용 |
|------|------|
| NOTIFY payload 크기 한도 | 8KB — 큰 JSONB 컬럼 strip + listener 재조회 |
| Listener 재연결 | asyncpg connection 끊김 감지 시 지수 백오프 재연결 (최대 5분) |
| Backend 다중 인스턴스 | 모든 인스턴스가 LISTEN — 동일 event 처리는 idempotency_key 로 dedup |
| DB 부하 | trigger 실행 자체는 빠르지만 매 lifecycle row 변경마다 NOTIFY — 트랜잭션 latency ~1ms 증가 |
| 모니터링 | Grafana 대시보드에 `pg_notification_queue_usage` 추적 추가 |

## 9. 리스크 / 미해결 사항

- **R1. NOTIFY 누락**: PostgreSQL NOTIFY 는 *최선 노력 (best-effort)* — listener 가 disconnect 된 동안 발행된 NOTIFY 는 손실
  - **완화**: listener startup 시 `SELECT FROM lifecycle_table WHERE txn_stat='PROC' AND updated_at > last_seen` 으로 누락 복구 (catch-up query)

- **R2. NOTIFY payload 8KB 초과 위험**: `ai_inference_txn.result_json` 등 큰 JSONB
  - **완화**: trigger 함수에서 큰 JSONB 컬럼 자동 strip + listener 가 PK 로 재조회

- **R3. 중복 처리**: dual delivery 단계에서 같은 event 가 두 번 처리될 위험
  - **완화**: subscriber 가 idempotency_key (기존 `_insp_dedup_seen` 패턴 활용)

- **R4. 외부 SQL UPDATE 에 의한 의도치 않은 lifecycle 진행**: 운영 도구가 실수로 row 를 바꾸면 backend 가 자동 반응 — 의도하지 않은 부작용
  - **완화**: trigger 함수에서 `current_setting('app.skip_notify', true)` 체크 — 운영 도구가 set local 으로 skip 가능

- **R5. 마이그레이션 시점의 dual write**: Phase 2 의 dual delivery 가 race condition 유발 가능
  - **완화**: feature flag `MGMT_USE_DB_EVENT_BRIDGE=1` 으로 옵트인 + 단계적 rollout

## 10. 검증 방법

- **단위 테스트**: trigger 함수가 정확한 payload 발행하는지 SQL 단위 테스트
- **통합 테스트**: 새 `test_db_event_listener_flow.py` — SQL UPDATE 만으로 lifecycle 진행 검증
- **e2e 회귀**: 기존 `test_ai_adapter_flow.py`, `watch_inspection_flow.py` 가 in-process / DB-origin 두 경로 모두 통과
- **부하 테스트**: 100 동시 item lifecycle 진행 시 NOTIFY queue overflow 없음을 확인
- **장애 시나리오**: listener disconnect / 재연결 / 누락 복구 동작 검증

## 11. 마일스톤

| Phase | 산출물 |
|-------|--------|
| 1 | `db_event_listener.py` + container startup wiring + 단위 테스트 |
| 2 | `insp_task_txn` trigger + e2e SQL-only 시뮬레이션 검증 |
| 3 | equip / trans / pp / item / ord_stat trigger 단계적 추가 |
| 4 | in-process pub/sub 정리 + DB trigger 단일 채널화 |

## 12. 참고 자료

- PostgreSQL `LISTEN/NOTIFY`: https://www.postgresql.org/docs/current/sql-notify.html
- asyncpg connection listener: https://magicstack.github.io/asyncpg/current/api/index.html#connection-objects
- 본 세션의 lifecycle 시뮬레이션 결과 (P0~P10 SQL UPDATE 강행 검증) — 본 SPEC 의 핵심 motivation
- 옵션 B AI 추론 패치 (PR #26, #27) — 본 SPEC 와 무관하게 선행 완료
