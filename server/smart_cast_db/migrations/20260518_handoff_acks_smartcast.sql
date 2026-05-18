-- A4: HandoffAck (SPEC-AMR-001) 을 smartcast 스키마로 이전.
--
-- Why:
--   handoff_pipeline.apply_handoff 가 HandoffAck 모델로 INSERT 하지만, legacy
--   public.handoff_acks 에 의존하면 backend connection 의 search_path 가 smartcast 로
--   고정될 때 (interface_service.app.seed.seed_database 의 SET search_path TO smartcast)
--   UndefinedTable: relation "handoff_acks" does not exist 가 발생.
--
--   smartcast 단일 스키마 정책에 맞춰 smartcast.handoff_acks 를 생성하고,
--   HandoffAck 모델은 __table_args__ = {"schema": "smartcast"} 로 명시한다.
--
-- 실행:
--   psql "$DATABASE_URL" -f migrations/20260518_handoff_acks_smartcast.sql
--   (사용자별 schema 사용 시 smartcast → 본인 schema 로 치환)
--
-- 정책:
--   - smartcast.transport_tasks 가 없으므로 task_id 의 FK 는 생략 (legacy 모델과 격리).
--   - 멱등 (IF NOT EXISTS) — 여러 번 실행해도 안전.

BEGIN;

CREATE TABLE IF NOT EXISTS smartcast.handoff_acks (
    id BIGSERIAL PRIMARY KEY,
    task_id VARCHAR,                              -- legacy transport_tasks.id 참조용 (FK 없음)
    zone TEXT NOT NULL,
    amr_id TEXT,
    ack_source TEXT NOT NULL,                     -- 'esp32_button' | 'debug_endpoint' | 'gui_override'
    operator_id TEXT,
    button_device_id TEXT,
    orphan_ack BOOLEAN NOT NULL DEFAULT FALSE,
    idempotency_key TEXT,
    metadata JSONB,
    ack_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_handoff_acks_zone_ack_at
    ON smartcast.handoff_acks (zone, ack_at DESC);

CREATE INDEX IF NOT EXISTS idx_handoff_acks_task_id
    ON smartcast.handoff_acks (task_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_handoff_acks_idempotency
    ON smartcast.handoff_acks (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

COMMIT;

-- 검증:
--   SELECT count(*) FROM smartcast.handoff_acks;
--   curl -X POST http://localhost:8000/api/debug/handoff-ack -H "Content-Type: application/json" -d '{}'
--   → HTTP 200, released=False/True 응답
