# Phase 4 분석 — in-memory pub/sub vs DB trigger 매핑

> **상태**: 분석 (DRAFT)
> **작성**: 2026-05-15
> **선행 PR**: #28 (Phase 1), #29 (Phase 2), #30 (Phase 3a/3b), #31 (Phase 3c)
> **본 문서는 Phase 4 의 정리 작업 plan — 실 코드 제거는 별도 PR (위험 차원)**

---

## 1. 배경

backend 의 `EventBridge.publish()` 호출 위치를 분석해, **DB trigger 로 대체 가능한 채널** 과 **in-process 로 유지해야 하는 채널** 을 분류한다.

SPEC: `docs/db_event_bridge/SPEC.md` §6 Phase 4
> in-process pub/sub 의 DB trigger 가능 부분 정리. DB row 와 무관한 event 만 in-process 유지.

## 2. EventBridge.publish 호출 카탈로그 (현재 backend)

| # | 호출 위치 | EventType | 발행 의미 |
|---|----------|-----------|----------|
| 1 | `event_gateway_servicer.py:229` | `RFID_SCANNED` / `HANDOFF_ACK` / `TOF1_ENTRY` (외부) | ESP32/Jetson/PyQt 의 외부 신호 relay |
| 2 | `container.py:497` | `ITEM_LOOKUP_RESULT` | RFID 후 item 조회 결과 → PyQt 응답 |
| 3 | `hardware_rpc.py:102` | `INSP_IMAGE_UPLOADED` | Jetson UploadInspectionImage → backend disk 저장 + image_path registry 업데이트 |
| 4 | `task_executor.py:393` | `ARM_RETURN_COMPLETED` | RA arm task 종결 |
| 5 | `state_manager.py:643` | `TASK_COMPLETED` | task 완료 — task_txn UPDATE 동반 |
| 6 | `state_manager.py:810` | `RESOURCE_AVAILABLE` | 자원 가용 신호 |
| 7 | `state_manager.py:1059` | `SUBTASK_COMPLETED` | subtask 완료 — 관련 task_txn UPDATE 동반 |
| 8 | `state_manager.py:1102` | `AMR_CHARGED` | AMR 충전 완료 |
| 9 | `db_event_listener.py:193` | `DB_ROW_CHANGED` | **신규 (PR #28)** — DB trigger NOTIFY 수신 |

## 3. DB trigger 매핑 분석

### 3.1 DB trigger 로 대체 가능 (4건)

| # | 현재 publish | 대체 DB trigger 신호 | 이유 |
|---|------------|-------------------|------|
| 5 | `TASK_COMPLETED` (state_manager) | `equip/trans/pp/insp_task_txn` UPDATE OF txn_stat | state_manager 가 task 완료 시 publish 직전에 task_txn 을 SUCC 로 UPDATE → 같은 trigger 가 발행됨 |
| 7 | `SUBTASK_COMPLETED` (state_manager) | task_txn UPDATE 또는 step 별 row | 위와 동일 |
| 4 | `ARM_RETURN_COMPLETED` (task_executor) | `equip_task_txn` UPDATE (RA 작업 종결) | RA arm task 종결 = equip_task_txn SUCC. trigger 가 매핑 |
| 8 | `AMR_CHARGED` (state_manager) | `equip_stat` UPDATE 또는 `chg_location_stat` UPDATE | 충전 완료 = res 상태 변경 동반 (확인 필요 — equip_stat 의 trigger 등록 필요할 수 있음) |

### 3.2 in-process 로 유지 (3건)

| # | EventType | 이유 |
|---|-----------|------|
| 1 | `RFID_SCANNED` / `HANDOFF_ACK` / `TOF1_ENTRY` | ESP32/Jetson 의 외부 신호 — DB row 변경 아닌 순수 신호. log_action_operator_* 에 INSERT 되긴 하지만 그건 부수 효과이고, 본 신호의 의미는 "외부 device 에서 일어난 event" |
| 2 | `ITEM_LOOKUP_RESULT` | RFID 스캔 후 backend 가 PyQt 에 응답하는 쿼리 결과 — DB 변경 아님 |
| 6 | `RESOURCE_AVAILABLE` | 자원 가용 신호 — 일부 시점에서는 in-memory 상태 변경만 (DB row 변경 동반 안 함). 단 일부 케이스에서 equip_stat UPDATE 동반 가능 — 추가 분석 |

### 3.3 하이브리드 — 일부 trigger 대체, 일부 in-process 보강 (1건)

| # | EventType | 분석 |
|---|-----------|------|
| 3 | `INSP_IMAGE_UPLOADED` | image 도착 자체는 DB 변경 아님 — backend disk 저장 + state_manager registry 업데이트만 발생. 후속 `insp_task_txn` INSERT (PROC) 가 곧 일어남 (이건 trigger 가 잡음). image_path 정보가 DB 에 없어 in-process 보강 필요 — Phase 3b 의 `state_manager.consume_inspection_image` 가 이 역할 수행. **결론: in-process 유지 + DB trigger 가 후속 lifecycle 진행** |

### 3.4 신규 (1건)

| # | EventType | 출처 |
|---|-----------|------|
| 9 | `DB_ROW_CHANGED` | PR #28 (Phase 1) 도입 — DB trigger 기반 단일 신호 |

## 4. 정리 우선순위 / 위험도

### Phase 4a (낮은 위험 — 단순 제거)
- **위치 5, 7** (`TASK_COMPLETED`, `SUBTASK_COMPLETED` in state_manager)
- 대체: equip/trans/pp/insp_task_txn UPDATE trigger 가 동일 신호 발행 (PR #29, #31 의 trigger 가 이미 등록됨)
- 작업: `task_executor` 의 외부 waiter 가 DB-origin event 도 수신하도록 router handler 추가 → in-memory publish 제거
- 검증: 기존 lifecycle 회귀 테스트 통과

### Phase 4b (중간 위험)
- **위치 4** (`ARM_RETURN_COMPLETED` in task_executor)
- 대체: `equip_task_txn` UPDATE (RA 작업 종결 시 equip_task SUCC)
- 작업: task_executor 의 emergency_return 흐름 검토, DB UPDATE 가 publish 시점과 정합한지 확인

### Phase 4c (조건부 정리 가능)
- **위치 6, 8** (`RESOURCE_AVAILABLE`, `AMR_CHARGED`)
- 대체 후보: `equip_stat` UPDATE — 단 trigger 미등록 상태. 별도 SQL 마이그레이션 필요
- 작업 큼 — 별도 PR 권장

### Phase 4d (정리 안 함 — in-process 유지)
- **위치 1, 2** — RFID_SCANNED/HANDOFF_ACK/TOF1_ENTRY (외부 신호), ITEM_LOOKUP_RESULT (쿼리 응답)
- 사유: DB row 변경 아닌 외부 신호 / 응답 채널 — DB trigger 로 대체 불가능

### Phase 4e (조건부 정리)
- **위치 3** — INSP_IMAGE_UPLOADED — 도착 자체 신호는 in-process 유지, 후속 lifecycle 은 trigger 가 담당 (Phase 3b 가 이미 처리)

## 5. 단계적 정리 plan

| Phase | 산출물 | 위험도 |
|-------|--------|--------|
| **4a** | task_executor 의 DB-origin event subscriber 추가 + TASK_COMPLETED / SUBTASK_COMPLETED in-memory publish 제거 | 낮음 (trigger 가 이미 발행 중) |
| **4b** | ARM_RETURN_COMPLETED in-memory publish 제거 + equip_task_txn UPDATE trigger 핸들러 추가 | 중간 |
| **4c** | equip_stat 마이그레이션 + AMR_CHARGED / RESOURCE_AVAILABLE 정리 | 높음 (마이그레이션 동반) |
| **4d** | 정리 완료 — in-process 채널은 RFID/HANDOFF/TOF1/ITEM_LOOKUP/INSP_IMAGE_UPLOADED 만 남음 | 낮음 (확인 작업) |

## 6. 핵심 제약 (정리 시 반드시 지킬 것)

- **dual delivery 안전성**: DB trigger publish + in-memory publish 가 동시에 일어날 가능성 — idempotency_key 또는 (table, PK, op) dedup 필수 (Phase 3a 의 `DbEventRouter` 가 이미 처리)
- **마이그레이션 롤백 가능성**: 각 Phase 정리는 dual delivery 단계 (양쪽 publish 유지) → in-memory publish 제거 단계로 2-step rollout
- **기존 회귀 테스트 통과**: 각 정리 PR 은 기존 lifecycle e2e 테스트 (test_ai_adapter_flow, watch_inspection_flow 등) 회귀 없음 검증
- **운영 활성화 의존**: `MGMT_DB_EVENT_BRIDGE_ENABLED=1` + `MGMT_DB_EVENT_TASK_DISPATCH=1` 이 운영에서 켜져 있어야 trigger 가 신호 발행

## 7. 본 PR (Phase 4 분석) 범위

**분석 문서만 작성** — 실 코드 변경 없음. 이유:
- 정리 작업 자체는 위험 (lifecycle 회귀 가능)
- Phase 4a~4d 각각이 별도 PR 권장
- 분석을 먼저 문서화해 팀 리뷰 + 우선순위 합의

## 8. 다음 PR 권장

**Phase 4a (낮은 위험 + 큰 가치)** 부터 진행:
- task_executor 의 DB_ROW_CHANGED subscriber 추가 (router 의 task_txn UPDATE handler)
- TASK_COMPLETED / SUBTASK_COMPLETED in-memory publish 제거 (dual delivery 후)
- 기존 e2e 회귀 테스트 통과 확인
