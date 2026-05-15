-- SPEC-DB-EVENT-BRIDGE-003: Phase 3c — 5개 lifecycle 테이블 trigger 확장
-- 실행: psql $DATABASE_URL -f server/main_service/scripts/migrate_db_event_bridge_phase3c.sql
-- 2026-05-15, 멱등 실행 가능
--
-- SPEC: docs/db_event_bridge/SPEC.md §6 Phase 3c
-- 선행 마이그레이션: migrate_db_event_bridge.sql (Phase 2 — notify_lifecycle_event 함수 + insp_task_txn trigger)
--
-- 본 마이그레이션은 동일 함수 smartcast.notify_lifecycle_event() 를 재사용하며,
-- 다음 5개 테이블에 AFTER INSERT/UPDATE trigger 를 추가한다:
--
--   equip_task_txn  — 주조(MM/POUR/DM) lifecycle
--   trans_task_txn  — 이송(ToPP/ToSTRG/ToSHIP/ToCHG) lifecycle
--   pp_task_txn     — 후처리 lifecycle
--   item            — cur_stat/is_defective 전이 (CREATED→CAST→WAIT_PP→...→READY_TO_SHIP)
--   ord_stat        — 발주 상태 (RCVD/APPR/MFG/SHIPPING/SHIP 등)
--
-- 각 trigger 는 INSERT 와 UPDATE 의 핵심 컬럼 변경 시 pg_notify('lifecycle_event', ...) 발행.
-- DbEventRouter 가 payload.table 별 handler 로 dispatch (PR #30 Phase 3a/3b).

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- equip_task_txn — 주조 task lifecycle (RA arm 작업)
-- ─────────────────────────────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_equip_task_txn_lifecycle_insert ON smartcast.equip_task_txn;
DROP TRIGGER IF EXISTS trg_equip_task_txn_lifecycle_update ON smartcast.equip_task_txn;

CREATE TRIGGER trg_equip_task_txn_lifecycle_insert
    AFTER INSERT ON smartcast.equip_task_txn
    FOR EACH ROW EXECUTE FUNCTION smartcast.notify_lifecycle_event();

CREATE TRIGGER trg_equip_task_txn_lifecycle_update
    AFTER UPDATE OF txn_stat ON smartcast.equip_task_txn
    FOR EACH ROW EXECUTE FUNCTION smartcast.notify_lifecycle_event();

-- ─────────────────────────────────────────────────────────────────────────────
-- trans_task_txn — TAT AMR 이송 lifecycle
-- ─────────────────────────────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_trans_task_txn_lifecycle_insert ON smartcast.trans_task_txn;
DROP TRIGGER IF EXISTS trg_trans_task_txn_lifecycle_update ON smartcast.trans_task_txn;

CREATE TRIGGER trg_trans_task_txn_lifecycle_insert
    AFTER INSERT ON smartcast.trans_task_txn
    FOR EACH ROW EXECUTE FUNCTION smartcast.notify_lifecycle_event();

CREATE TRIGGER trg_trans_task_txn_lifecycle_update
    AFTER UPDATE OF txn_stat ON smartcast.trans_task_txn
    FOR EACH ROW EXECUTE FUNCTION smartcast.notify_lifecycle_event();

-- ─────────────────────────────────────────────────────────────────────────────
-- pp_task_txn — 후처리 lifecycle
-- ─────────────────────────────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_pp_task_txn_lifecycle_insert ON smartcast.pp_task_txn;
DROP TRIGGER IF EXISTS trg_pp_task_txn_lifecycle_update ON smartcast.pp_task_txn;

CREATE TRIGGER trg_pp_task_txn_lifecycle_insert
    AFTER INSERT ON smartcast.pp_task_txn
    FOR EACH ROW EXECUTE FUNCTION smartcast.notify_lifecycle_event();

CREATE TRIGGER trg_pp_task_txn_lifecycle_update
    AFTER UPDATE OF txn_stat ON smartcast.pp_task_txn
    FOR EACH ROW EXECUTE FUNCTION smartcast.notify_lifecycle_event();

-- ─────────────────────────────────────────────────────────────────────────────
-- item — cur_stat / is_defective 전이 추적
--   생산 시작 시 INSERT, 각 stage 전이 시 cur_stat UPDATE, 검사 결과 is_defective UPDATE.
-- ─────────────────────────────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_item_lifecycle_insert ON smartcast.item;
DROP TRIGGER IF EXISTS trg_item_lifecycle_update ON smartcast.item;

CREATE TRIGGER trg_item_lifecycle_insert
    AFTER INSERT ON smartcast.item
    FOR EACH ROW EXECUTE FUNCTION smartcast.notify_lifecycle_event();

CREATE TRIGGER trg_item_lifecycle_update
    AFTER UPDATE OF cur_stat, is_defective ON smartcast.item
    FOR EACH ROW EXECUTE FUNCTION smartcast.notify_lifecycle_event();

-- ─────────────────────────────────────────────────────────────────────────────
-- ord_stat — 발주 상태 전이 (RCVD/APPR/MFG/SHIPPING/SHIP 등)
-- ─────────────────────────────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_ord_stat_lifecycle_insert ON smartcast.ord_stat;
DROP TRIGGER IF EXISTS trg_ord_stat_lifecycle_update ON smartcast.ord_stat;

CREATE TRIGGER trg_ord_stat_lifecycle_insert
    AFTER INSERT ON smartcast.ord_stat
    FOR EACH ROW EXECUTE FUNCTION smartcast.notify_lifecycle_event();

CREATE TRIGGER trg_ord_stat_lifecycle_update
    AFTER UPDATE OF ord_stat ON smartcast.ord_stat
    FOR EACH ROW EXECUTE FUNCTION smartcast.notify_lifecycle_event();

COMMIT;

-- ─────────────────────────────────────────────────────────────────────────────
-- 검증 절차 (수동):
--
--   Session A:
--     psql $DATABASE_URL -c "LISTEN lifecycle_event;"
--
--   Session B (예시 — equip_task_txn):
--     INSERT INTO smartcast.equip_task_txn (item_id, task_type, res_id, txn_stat, req_at, start_at)
--         VALUES (<item_id>, 'MM', 'MAT', 'PROC', now(), now()) RETURNING txn_id;
--
--   Session A 에 Asynchronous notification 출력 (equip_task_txn_insert) 확인.
--
-- 자동 검증:
--   server/main_service/tests/integration/test_db_event_bridge_phase3c.py
-- ─────────────────────────────────────────────────────────────────────────────

-- 롤백:
--   BEGIN;
--   DROP TRIGGER IF EXISTS trg_equip_task_txn_lifecycle_insert ON smartcast.equip_task_txn;
--   DROP TRIGGER IF EXISTS trg_equip_task_txn_lifecycle_update ON smartcast.equip_task_txn;
--   DROP TRIGGER IF EXISTS trg_trans_task_txn_lifecycle_insert ON smartcast.trans_task_txn;
--   DROP TRIGGER IF EXISTS trg_trans_task_txn_lifecycle_update ON smartcast.trans_task_txn;
--   DROP TRIGGER IF EXISTS trg_pp_task_txn_lifecycle_insert ON smartcast.pp_task_txn;
--   DROP TRIGGER IF EXISTS trg_pp_task_txn_lifecycle_update ON smartcast.pp_task_txn;
--   DROP TRIGGER IF EXISTS trg_item_lifecycle_insert ON smartcast.item;
--   DROP TRIGGER IF EXISTS trg_item_lifecycle_update ON smartcast.item;
--   DROP TRIGGER IF EXISTS trg_ord_stat_lifecycle_insert ON smartcast.ord_stat;
--   DROP TRIGGER IF EXISTS trg_ord_stat_lifecycle_update ON smartcast.ord_stat;
--   COMMIT;
