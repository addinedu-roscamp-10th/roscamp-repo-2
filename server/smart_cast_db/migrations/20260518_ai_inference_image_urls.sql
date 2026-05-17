-- 2026-05-18: AiInferenceTxn 에 검사 결과 이미지 URL 컬럼 추가
--
-- 배경:
--   AI /predict 응답의 segmented_image / result_image (base64 PNG) 를
--   backend HttpImageServer 디스크에 저장하고, 외부 fetch URL 만 DB 에 보관.
--   PyQt vision_feed 가 본 URL 로 직접 GET 하여 결과 이미지를 표시.
--
-- 적용:
--   psql $DATABASE_URL -f server/smart_cast_db/migrations/20260518_ai_inference_image_urls.sql
--
-- 롤백:
--   ALTER TABLE smartcast.ai_inference_txn
--       DROP COLUMN IF EXISTS segmented_image_url,
--       DROP COLUMN IF EXISTS result_image_url;

BEGIN;

ALTER TABLE smartcast.ai_inference_txn
    ADD COLUMN IF NOT EXISTS segmented_image_url TEXT,
    ADD COLUMN IF NOT EXISTS result_image_url    TEXT;

COMMIT;
