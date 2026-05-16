# 멀티 스키마 격리 가이드 — 사용자별 RDS 스키마 운영

> **상태**: 운영 가이드
> **작성**: 2026-05-15
> **대상**: AWS RDS Casting DB 의 backend 개발자

---

## 1. 배경

AWS RDS `Casting` DB 는 단일 인스턴스에 **사용자별 격리 스키마** 를 운영합니다. 한 명이 시뮬레이션 / e2e 테스트를 돌릴 때 다른 팀원의 작업이 깨지지 않도록 격리.

기존 스키마:
- `smartcast` — 공유 통합 (운영 / 통합 검증)
- `inbean` — kiminbean 전용
- `yejin` — yejin 전용
- (확장) 새 사용자는 본인 ID 스키마를 RDS 에 생성

각 스키마는 동일 테이블 구조 + master 시드 데이터를 가짐.

## 2. backend 의 스키마 결정 메커니즘

backend 는 다음 3 환경변수로 schema 를 결정합니다 — `.env.local` 에 동일 값으로 set:

| 환경변수 | 사용처 | 기본값 |
|---------|--------|--------|
| `SMARTCAST_SCHEMA` | ORM (`smart_cast_db/models/_base.py`) — Item / AiInferenceTxn 등 모든 모델 | `smartcast` |
| `DB_SCHEMA` | scripts (`smart_cast_db/scripts/01_create_tables.py`, `_db.py`) | `smartcast` |
| `PG_SCHEMA` | e2e watcher (`watch_inspection_flow.py`) — `SMARTCAST_SCHEMA` 의 fallback | `smartcast` |

**3개를 모두 동일 값으로 set** 하지 않으면 ORM / scripts / e2e 가 서로 다른 스키마를 가리켜 버그가 발생합니다.

## 3. 스키마 전환 절차

### 3.1 사용자 스키마 생성 (최초 1회)

본인 스키마가 RDS 에 없으면 먼저 생성 + master 시드:

```sql
-- 새 사용자 스키마 생성
CREATE SCHEMA <user>;

-- 기존 smartcast 의 master 시드 + 테이블 구조를 복사 (방법 1: pg_dump)
pg_dump -h <RDS_HOST> -U postgres -d Casting -n smartcast --schema-only \
    | sed 's/smartcast\./<user>./g' \
    | psql -h <RDS_HOST> -U postgres -d Casting

-- master 시드만 (category / product / pattern_master / ai_model / res / zone)
psql -d Casting -c "
INSERT INTO <user>.category SELECT * FROM smartcast.category;
INSERT INTO <user>.product  SELECT * FROM smartcast.product;
-- ... 이하 master 테이블들
"
```

### 3.2 trigger 마이그레이션 적용 (Phase 2 + 3c)

본인 스키마에 `notify_lifecycle_event()` 함수 + 6 table × 2 trigger 등록:

```bash
# .env.local 의 DATABASE_URL 을 psql 호환 dsn 으로 변환 (postgresql+psycopg:// → postgresql://)
PSQL_DSN=$(echo "$DATABASE_URL" | sed -E 's|^postgresql\+[a-z0-9_]+://|postgresql://|')

# Phase 2 (insp_task_txn) + Phase 3c (5 table) 모두 적용
# 기존 SQL 은 smartcast 하드코딩 — sed 로 본인 스키마로 치환
for sql in server/main_service/scripts/migrate_db_event_bridge.sql \
           server/main_service/scripts/migrate_db_event_bridge_phase3c.sql; do
    sed 's/smartcast\./<user>./g; s/CREATE OR REPLACE FUNCTION smartcast/CREATE OR REPLACE FUNCTION <user>/g' "$sql" \
        | psql "$PSQL_DSN"
done

# 등록 확인 (12 trigger 기대)
psql "$PSQL_DSN" -c "
SELECT relname, count(*) FROM pg_trigger t JOIN pg_class c ON t.tgrelid=c.oid
WHERE tgname LIKE '%lifecycle%' AND c.relnamespace=(SELECT oid FROM pg_namespace WHERE nspname='<user>')
GROUP BY relname ORDER BY relname;"
```

### 3.3 backend 환경변수 변경

`.env.local` 에서 3 환경변수를 본인 스키마로 set:

```bash
SMARTCAST_SCHEMA=<user>
DB_SCHEMA=<user>
PG_SCHEMA=<user>
```

### 3.4 backend 재시작 + 검증

```bash
# 기존 backend 종료
pkill -f "main_service/server.py"
pkill -f "uvicorn.*main_service"

# 재시작
./scripts/run-management.sh > logs/management.log 2>&1 &
./scripts/run-backend.sh > logs/backend.log 2>&1 &

# 검증
sleep 5
curl -s http://127.0.0.1:8000/api/products | python3 -c "import json,sys; print(f'products count: {len(json.load(sys.stdin))}')"

# ORM SCHEMA 변수 직접 확인
PYTHONPATH=src:src/interface_service:src/management_service:.. \
    .venv/bin/python -c "from smart_cast_db.models._base import SCHEMA; print(f'SCHEMA={SCHEMA}')"
```

## 4. 운영 활성화 (DB Event Bridge)

SPEC: `docs/db_event_bridge/SPEC.md`

`.env.local` 에 추가:
```bash
MGMT_DB_EVENT_BRIDGE_ENABLED=1
MGMT_DB_EVENT_TASK_DISPATCH=1
```

backend 재시작 → listener + dispatcher 자동 활성. 본인 스키마의 trigger 가 발행하는 NOTIFY 만 수신 (다른 사용자 스키마와 격리).

## 5. 주의사항

- **3 환경변수를 모두 동일 값으로 set** — 한 곳만 바꾸면 ORM/scripts/e2e 가 다른 스키마 가리킴
- **master 시드 정합성** — 본인 스키마의 product / ai_model / res 등 시드가 smartcast 와 동일해야 ORM 동작 정합. 변경 시 본인 스키마만 영향
- **trigger 적용 후 backend 재시작 필수** — listener 가 startup 시 LISTEN 채널 구독
- **dual delivery 안전성** — 동일 backend 가 in-memory pub/sub 와 DB trigger 둘 다 수신할 수 있어 `DbEventRouter` 의 dedup cache (60s TTL) 가 차단 (Phase 3a)
- **다른 사용자 스키마 영향 없음** — 본인 스키마의 trigger 는 본인 backend 만 LISTEN 채널 구독 시 받음

## 6. 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| `relation "<schema>.item" does not exist` | 본인 스키마에 테이블 미생성 | §3.1 master 시드 + 테이블 구조 복사 |
| backend 가 'smartcast' 데이터 조회 | 3 환경변수 중 일부 미설정 | §3.3 3개 모두 동일 값 |
| `notify_lifecycle_event` 함수 없음 | trigger 마이그레이션 미적용 | §3.2 적용 |
| 다른 사용자 스키마 INSERT 가 NOTIFY 옴 | 동일 DB 에서 LISTEN 은 단일 채널 — 모든 publish 수신 | DbEventRouter 가 payload.schema 로 필터링 (Phase 4 권장) |

## 7. 참고

- SPEC: `docs/db_event_bridge/SPEC.md`
- Phase 4 분석: `docs/db_event_bridge/PHASE4_ANALYSIS.md`
- ORM SCHEMA 정의: `server/smart_cast_db/models/_base.py:27`
- Phase 2 마이그레이션: `server/main_service/scripts/migrate_db_event_bridge.sql`
- Phase 3c 마이그레이션: `server/main_service/scripts/migrate_db_event_bridge_phase3c.sql`
