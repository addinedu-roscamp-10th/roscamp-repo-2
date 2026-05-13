-- 2026-05-12 e2e 사이클 item_id=2 재진입 reset
--
-- 목표: 본 세션 검증 publish (idem='test-handoff-*') 로 apply_handoff 가
--       실 실행되어 item.cur_stat='PP' + pp_task_txn 2건 + trans 76 SUCC + TAT1 SUCC
--       으로 변경된 상태를 사용자 실 푸시 버튼 진입 가능 상태로 원상복귀 +
--       새 ToPP/PROC trans_task_txn INSERT.
--
-- 적용:
--   set -a; . server/main_service/.env.local; set +a
--   PG_URL=$(echo "$DATABASE_URL" | sed -E 's|postgresql\+psycopg(2?)://|postgresql://|')
--   psql "$PG_URL" -v ON_ERROR_STOP=1 -f scripts/e2e/migrations/2026-05-12_e2e_item2_reset.sql

BEGIN;

-- (1) 검증 publish 로 생성된 pp_task_txn 2건 (표면 연마 QUE + 방청 코팅 QUE) DELETE
-- 새 사이클에서 apply_handoff 가 다시 INSERT 함 (unique 제약 없으므로 중복 방지용 cleanup).
DELETE FROM smartcast.pp_task_txn WHERE item_id=2 AND txn_stat='QUE';

-- (2) 검증으로 SUCC 처리된 trans_task_txn 76 → FAIL
-- 새 ToPP/PROC 행을 INSERT 하기 전에 기존 SUCC 행을 FAIL 로 마감.
UPDATE smartcast.trans_task_txn
SET txn_stat='FAIL', end_at=NOW()
WHERE trans_task_txn_id=76 AND txn_stat='SUCC';

-- (3) item_id=2 cur_stat 원상복귀 (PP → CREATED)
UPDATE smartcast.item
SET cur_stat='CREATED', updated_at=NOW()
WHERE item_id=2;

-- (4) trans_stat TAT1 cur_stat 원상복귀 (SUCC → ALLOC)
UPDATE smartcast.trans_stat
SET cur_stat='ALLOC', updated_at=NOW()
WHERE res_id='TAT1';

-- (5) 검증 publish 로 INSERT 된 HandoffAck 행 DELETE
-- idempotency_key 가 'test-handoff%' 패턴 (사용자 실 ESP32 idem 패턴과 다름).
DELETE FROM smartcast.log_action_operator_handoff_acks
WHERE idempotency_key LIKE 'test-handoff%';

-- (6) 검증 publish 로 INSERT 된 RfidScanLog 행 DELETE (혹시 있다면)
DELETE FROM smartcast.log_action_operator_rfid_scan
WHERE idempotency_key LIKE 'test-%';

-- (7) item_id=2 의 새 ToPP/PROC trans_task_txn INSERT
-- 사용자가 실 GPIO33 푸시 버튼 누르면 apply_handoff 가 FIFO 첫 PROC pick → 이 행 잡음.
-- trans_stat.TAT1.cur_stat='ALLOC' 상태 + battery 78% → handoff 정상 진입 가능.
INSERT INTO smartcast.trans_task_txn (trans_id, task_type, txn_stat, item_id, ord_id, req_at)
VALUES ('TAT1', 'ToPP', 'PROC', 2, 1, NOW());

COMMIT;

-- 적용 후 검증 (수동):
-- 1. item_id=2: cur_stat='CREATED' (이전 PP 에서 복귀):
--    SELECT item_id, ord_id, cur_stat, cur_res, is_defective FROM smartcast.item WHERE item_id=2;
--
-- 2. pp_task_txn item_id=2: 0 rows (cleanup 됨):
--    SELECT COUNT(*) FROM smartcast.pp_task_txn WHERE item_id=2;
--
-- 3. ToPP/PROC trans_task_txn 단 1행 (방금 INSERT 된 신규 row, 새 trans_task_txn_id):
--    SELECT trans_task_txn_id, trans_id, item_id, task_type, txn_stat, req_at
--    FROM smartcast.trans_task_txn WHERE task_type='ToPP' AND txn_stat='PROC';
--
-- 4. trans_stat.TAT1: cur_stat='ALLOC':
--    SELECT res_id, cur_stat, battery_pct FROM smartcast.trans_stat WHERE res_id='TAT1';
--
-- 5. HandoffAck 'test-handoff%' 0 rows:
--    SELECT COUNT(*) FROM smartcast.log_action_operator_handoff_acks
--    WHERE idempotency_key LIKE 'test-handoff%';
