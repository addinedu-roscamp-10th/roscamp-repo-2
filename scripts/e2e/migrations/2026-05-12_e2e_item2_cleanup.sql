-- 2026-05-12 e2e 사이클 item_id=2 setup
--
-- 목표: ord_id=1 의 item_id=2 (RFID 04:2D:EE, NDEF 'order_1_item_20260417_2') 를
--       새 사이클로 진입시키기 위한 잔존 PROC 정리 + ToPP/PROC trans_task_txn INSERT.
--
-- 적용 명령:
--   set -a; . server/main_service/.env.local; set +a
--   PG_URL=$(echo "$DATABASE_URL" | sed -E 's|postgresql\+psycopg(2?)://|postgresql://|')
--   psql "$PG_URL" -f scripts/e2e/migrations/2026-05-12_e2e_item2_cleanup.sql
--
-- 적용 전 backup 권장:
--   psql "$PG_URL" -c "SELECT trans_task_txn_id, trans_id, item_id, task_type, txn_stat \
--     FROM smartcast.trans_task_txn WHERE txn_stat='PROC';"
--   psql "$PG_URL" -c "SELECT txn_id, item_id, task_type, txn_stat \
--     FROM smartcast.equip_task_txn WHERE txn_stat='PROC';"

BEGIN;

-- (1) 잔존 ToPP/PROC trans_task_txn FAIL 처리
-- apply_handoff(handoff_pipeline.py:74-95) 는 FIFO 첫 ToPP/PROC pick — 잔존 행이
-- 있으면 사용자가 의도한 item_id=2 가 아니라 옛 사이클의 item_id (20, 1) 을 잡음.
-- trans_task_txn_id 는 본 세션 진단 (2026-05-12) 결과:
--   - 38: item_id=20, ToPP, PROC, req_at=2026-05-11 21:40 (이전 사이클)
--   - 70: item_id=1,  ToPP, PROC, req_at=2026-05-12 16:18 (오늘 옛 사이클)
UPDATE smartcast.trans_task_txn
SET txn_stat='FAIL', end_at=NOW()
WHERE trans_task_txn_id IN (38, 70)
  AND txn_stat='PROC';

-- (2) 잔존 equip_task_txn PROC FAIL 처리
-- txn_id=425 (item_id=1, PP, PROC) — 이전 세션 INSP timeout 으로 PP task 가
-- PROC 상태로 stuck. task_executor 가 살아있는 PROC 로 인식해서 새 사이클의 PP task
-- 와 충돌할 위험 → FAIL 처리.
UPDATE smartcast.equip_task_txn
SET txn_stat='FAIL', end_at=NOW()
WHERE txn_id=425
  AND txn_stat='PROC';

-- (3) item_id=2 의 새 ToPP/PROC trans_task_txn INSERT
-- AMR TAT1 (trans_stat.cur_stat='ALLOC' + battery_pct=78%) 할당. apply_handoff 가
-- FIFO 첫 PROC pick → t.res_id (synonym trans_id) 로 TransStat 가져와 cur_stat='SUCC'
-- 으로 전이 + item.flow_stat='PP' (synonym cur_stat='PP') + pp_task_txn QUE INSERT.
INSERT INTO smartcast.trans_task_txn (trans_id, task_type, txn_stat, item_id, ord_id, req_at)
VALUES ('TAT1', 'ToPP', 'PROC', 2, 1, NOW());

COMMIT;

-- 적용 후 검증 (수동):
-- 1. ToPP/PROC 가 단 1행 (item_id=2 의 새 행) 만 남았는지:
--    SELECT trans_task_txn_id, trans_id, item_id, task_type, txn_stat, req_at
--    FROM smartcast.trans_task_txn WHERE task_type='ToPP' AND txn_stat='PROC';
--
-- 2. AMR TAT1 의 trans_stat 가 backend handoff 픽업 가능한 상태인지:
--    SELECT res_id, cur_stat, battery_pct FROM smartcast.trans_stat WHERE res_id='TAT1';
--
-- 3. item_id=2 의 현재 상태 (cur_stat='CREATED' 그대로):
--    SELECT item_id, ord_id, cur_stat, cur_res, is_defective FROM smartcast.item WHERE item_id=2;
--
-- 4. ord_pp_map (필요옵션 매핑) 정상 — 표면 연마 + 방청 코팅:
--    SELECT m.map_id, m.pp_id, o.pp_nm, o.extra_cost FROM smartcast.ord_pp_map m
--    JOIN smartcast.pp_options o ON o.pp_id=m.pp_id WHERE m.ord_id=1;
--
-- 5. ord_id=1 의 잔존 equip PROC 없어야:
--    SELECT txn_id, item_id, task_type, txn_stat FROM smartcast.equip_task_txn
--    WHERE txn_stat='PROC';
