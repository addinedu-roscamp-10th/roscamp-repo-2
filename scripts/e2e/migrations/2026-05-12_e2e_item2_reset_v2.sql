-- 2026-05-12 item_id=2 사이클 완전 reset (full cycle 종료 후 재진입)
--
-- 이전 사이클 (HANDOFF_ACK 19:33 ~ AI inference 20:20) 결과 모두 cleanup +
-- 새 ToPP/PROC trans_task_txn INSERT → 빨간 푸시 버튼부터 다시 진행 가능.
--
-- 적용:
--   psql "$PG_URL" -v ON_ERROR_STOP=1 -f scripts/e2e/migrations/2026-05-12_e2e_item2_reset_v2.sql

BEGIN;

-- (1) insp_stat DELETE
DELETE FROM smartcast.insp_stat
WHERE insp_txn_id IN (SELECT txn_id FROM smartcast.insp_task_txn WHERE item_id=2);

-- (2) ai_inference_txn DELETE
DELETE FROM smartcast.ai_inference_txn
WHERE insp_txn_id IN (SELECT txn_id FROM smartcast.insp_task_txn WHERE item_id=2);

-- (3) insp_task_txn DELETE (item_id=2 의 모든 row — insp_txn_id=31 등)
DELETE FROM smartcast.insp_task_txn WHERE item_id=2;

-- (4) pp_task_txn DELETE (표면 연마 + 방청 코팅 SUCC 행)
DELETE FROM smartcast.pp_task_txn WHERE item_id=2;

-- (5) equip_task_txn item_id=2 의 ToINSP (SUCC 또는 PROC) DELETE
-- MM (QUE) row 들은 옛 잔여 — 그대로 둠 (apply_handoff 가 pick 안 함).
DELETE FROM smartcast.equip_task_txn WHERE item_id=2 AND task_type='ToINSP';

-- (6) equip_stat CONV1 reset
UPDATE smartcast.equip_stat
SET item_id=NULL, txn_type=NULL, cur_stat='IDLE', updated_at=NOW()
WHERE res_id='CONV1';

-- (7) trans_task_txn 78 SUCC → FAIL (이전 사이클 마감)
UPDATE smartcast.trans_task_txn
SET txn_stat='FAIL', end_at=NOW()
WHERE trans_task_txn_id=78 AND txn_stat='SUCC';

-- (8) trans_stat TAT1 reset (SUCC → ALLOC)
UPDATE smartcast.trans_stat
SET cur_stat='ALLOC', updated_at=NOW()
WHERE res_id='TAT1';

-- (9) item_id=2 reset (cur_stat WAIT_INSP → CREATED, is_defective false → NULL)
UPDATE smartcast.item
SET cur_stat='CREATED', is_defective=NULL, updated_at=NOW()
WHERE item_id=2;

-- (10) HandoffAck DELETE (ESP32 GPIO33 푸시버튼 기록 + 검증 publish 잔존)
DELETE FROM smartcast.log_action_operator_handoff_acks
WHERE button_device_id='ESP-CONVEYOR-01'
   OR idempotency_key LIKE 'ESP-CONVEYOR-01:%'
   OR idempotency_key LIKE 'test-handoff%';

-- (11) 새 ToPP/PROC trans_task_txn INSERT (다음 사이클 apply_handoff 가 pick)
INSERT INTO smartcast.trans_task_txn (trans_id, task_type, txn_stat, item_id, ord_id, req_at)
VALUES ('TAT1', 'ToPP', 'PROC', 2, 1, NOW());

COMMIT;

-- 적용 후 검증:
-- 1. item_id=2: cur_stat='CREATED', is_defective=NULL
-- 2. pp/insp/ai/insp_stat: 모두 0 rows
-- 3. equip_task_txn ToINSP item_id=2: 0 rows (MM QUE 잔여만)
-- 4. ToPP/PROC trans_task_txn: 단 1행 (item_id=2 의 신규)
-- 5. equip_stat CONV1: cur_stat='IDLE'
-- 6. trans_stat TAT1: cur_stat='ALLOC'
