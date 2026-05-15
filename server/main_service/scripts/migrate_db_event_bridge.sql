-- SPEC-DB-EVENT-BRIDGE-002: Phase 2 — insp_task_txn lifecycle trigger + NOTIFY 함수
-- 실행: psql $DATABASE_URL -f server/main_service/scripts/migrate_db_event_bridge.sql
-- 2026-05-15, 멱등 실행 가능
--
-- SPEC: docs/db_event_bridge/SPEC.md §4~5
-- 선행 PR: #28 (Phase 1 — DbEventListener 골격)
--
-- 본 마이그레이션의 핵심 동작:
--   AFTER INSERT / AFTER UPDATE OF txn_stat ON smartcast.insp_task_txn
--     → pg_notify('lifecycle_event', json_payload)
--   → backend DbEventListener LISTEN
--   → EventBridge.publish(EventType.DB_ROW_CHANGED, payload={table, op, row, ...})
--   → 후속 subscriber (Phase 3 에서 도입) 가 payload.table='insp_task_txn' 으로 분기
--
-- Phase 2 범위 — insp_task_txn 만. 나머지 테이블(equip/trans/pp/item/ord_stat) trigger 는 Phase 3.

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1) NOTIFY 발행 함수 (모든 lifecycle 테이블 공유)
--    8KB payload 한도 회피: 큰 JSONB 컬럼 strip 은 Phase 3 (필요 시) 추가.
--    현 insp_task_txn 스키마는 row 전체 < 8KB 보장됨.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION smartcast.notify_lifecycle_event() RETURNS trigger AS $$
DECLARE
    event_name TEXT;
    payload    JSON;
    row_json   JSON;
    old_json   JSON;
BEGIN
    event_name := TG_TABLE_NAME || '_' || lower(TG_OP);
    row_json := row_to_json(NEW);
    IF TG_OP = 'UPDATE' THEN
        old_json := row_to_json(OLD);
    END IF;

    payload := json_build_object(
        'event',   event_name,
        'schema',  TG_TABLE_SCHEMA,
        'table',   TG_TABLE_NAME,
        'op',      TG_OP,
        'row',     row_json,
        'old_row', old_json,
        'at',      to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
    );

    PERFORM pg_notify('lifecycle_event', payload::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION smartcast.notify_lifecycle_event() IS
'AFTER INSERT/UPDATE trigger function — pg_notify lifecycle_event channel. SPEC-DB-EVENT-BRIDGE-001 Phase 1+2.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 2) insp_task_txn trigger (멱등 — 기존 trigger 가 있으면 DROP 후 재생성)
-- ─────────────────────────────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_insp_task_txn_lifecycle_insert ON smartcast.insp_task_txn;
DROP TRIGGER IF EXISTS trg_insp_task_txn_lifecycle_update ON smartcast.insp_task_txn;

CREATE TRIGGER trg_insp_task_txn_lifecycle_insert
    AFTER INSERT ON smartcast.insp_task_txn
    FOR EACH ROW EXECUTE FUNCTION smartcast.notify_lifecycle_event();

CREATE TRIGGER trg_insp_task_txn_lifecycle_update
    AFTER UPDATE OF txn_stat ON smartcast.insp_task_txn
    FOR EACH ROW EXECUTE FUNCTION smartcast.notify_lifecycle_event();

COMMIT;

-- ─────────────────────────────────────────────────────────────────────────────
-- 검증 절차 (수동, 별도 두 psql 세션 필요):
--
--   Session A:
--     psql $DATABASE_URL -c "LISTEN lifecycle_event;"
--     (가만히 기다림 — NOTIFY 수신 대기)
--
--   Session B:
--     INSERT INTO smartcast.insp_task_txn (item_id, txn_stat, req_at, start_at)
--         VALUES (28, 'PROC', now(), now()) RETURNING txn_id;
--     UPDATE smartcast.insp_task_txn SET txn_stat='SUCC', end_at=now()
--         WHERE txn_id=<위 반환값>;
--
--   Session A 에서 2건의 Asynchronous notification 이 출력되어야 함:
--     - insp_task_txn_insert  (txn_stat=PROC)
--     - insp_task_txn_update  (txn_stat=SUCC)
--
-- 자동 검증:
--   server/main_service/tests/integration/test_db_event_bridge_phase2.py
-- ─────────────────────────────────────────────────────────────────────────────

-- 롤백 (수동):
--   BEGIN;
--   DROP TRIGGER IF EXISTS trg_insp_task_txn_lifecycle_insert ON smartcast.insp_task_txn;
--   DROP TRIGGER IF EXISTS trg_insp_task_txn_lifecycle_update ON smartcast.insp_task_txn;
--   DROP FUNCTION IF EXISTS smartcast.notify_lifecycle_event();
--   COMMIT;
