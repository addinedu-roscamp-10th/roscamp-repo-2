# AI 검사 결과 이미지 영속화 — 설계 PLAN

> Branch: `feat/ai-inspection-image-persistence`
> Base: dev (PR #26/#27/#36 머지 후 상태)
> 작성일: 2026-05-18

---

## 1. 목표

AI 서버 `/predict` 응답의 `segmented_image` / `result_image` (base64 PNG) 를 backend 디스크에 저장하고 DB 영속화하여, PyQt vision_feed 가 실 결과 이미지를 표시하도록 한다.

PR #26/#27 가 schema 매핑·DB 정합 앞단을 해결했고, 본 트랙은 **결과 이미지 처리·노출 단**을 완성한다.

---

## 2. 현재 상태 (사전 인프라)

| 구성요소 | 위치 | 상태 |
|---|---|---|
| 검사 이미지 디스크 저장 | `services/command/inspection_image_sink_command.py` | ✅ 카메라 캡처 원본만 저장 |
| HttpImageServer | `services/http_image_server.py` | ✅ `MGMT_INSP_IMAGE_SAVE_DIR` 의 `{item_id}/{filename}` 을 HTTP 18800 으로 서빙. **2-segment 라우트** |
| AI 응답 정규화 | `services/command/ai_inference_command.py` | ✅ `segmented_image_b64` / `result_image_b64` 추출하나 dataclass 까지만, dead field |
| DB 모델 (옵션 B) | `smart_cast_db/models/inspection.py` `AiInferenceTxn` (line 86) | ⚠️ 이미지 경로/URL 컬럼 없음 |
| 응답 schema | `interface_service/app/schemas/schemas.py` `InspTaskTxnOut` (line 275) | ⚠️ `image_id` / `image_url` 필드 없음 |
| PyQt vision_feed | `ui/pyqt/.../widgets/vision_feed.py` | ✅ `load_image_for(image_id)` → `{INSPECTION_IMAGE_BASE_URL}/{image_id}.jpg` **1-segment fetch**. default base url 은 AI 서버 8080 (잘못된 가정) |

---

## 3. 핵심 설계 결정 (4개)

### D1. 이미지 호스팅 주체

| 옵션 | 장점 | 단점 |
|---|---|---|
| **A. backend HttpImageServer (권장)** | 기존 인프라 재사용, 검사 원본·결과 이미지 단일 채널, AI 서버 외부 호스팅 가정 불필요 | PyQt `INSPECTION_IMAGE_BASE_URL` 변경 필요 (8080 → 18800) |
| B. AI 서버 자체 호스팅 | PyQt 현 default 유지 | AI 서버에 8080 정적 서빙 합의/구현 필요 (없음) |

→ **A 채택**. 메모리의 "PyQt↔Backend 단일 채널" 정책과 정합.

### D2. URL 패턴 (HttpImageServer route)

| 옵션 | URL |
|---|---|
| **A. 2-segment 유지 + PyQt 변경 (권장)** | `/inspections/{item_id}/{filename}` (기존) → PyQt 의 vision_feed 가 `image_url` 완전 URL 을 그대로 fetch |
| B. 1-segment 신규 route | `/images/{image_id}.png` → image_id 에 item_id 인코딩 필요 |

→ **A 채택**. backend 응답에 `image_url` 완전 URL 을 포함시키고 PyQt 는 그대로 GET. backend 와 PyQt 결합도 최소화.

### D3. 표시할 이미지 (segmented vs result)

| 옵션 | 결과 |
|---|---|
| **A. result_image 1장만 (권장)** | PyQt vision_feed 슬롯 1개 정합. AI 의 "최종 결과 이미지" 의도 일치 |
| B. 둘 다 표시 | PyQt UI 토글/탭 추가 필요 (범위 확대) |

→ **A 채택**. segmented 도 디스크 저장은 하되, PyQt 노출은 result 1장. 후속 트랙에서 확장 가능.

### D4. DB 컬럼 위치

| 옵션 | 위치 |
|---|---|
| **A. AiInferenceTxn 에 컬럼 2개 추가 (권장)** | `segmented_image_url TEXT NULL`, `result_image_url TEXT NULL`. 명시적, 검색 가능 |
| B. AiInferenceTxn.result_json JSONB 안에 키 포함 | DDL 변경 없음. 그러나 추출 불편 |
| C. 별도 테이블 insp_image_artifact | 정규화. DDL 신규 테이블 |

→ **A 채택**. URL 만 보관 (디스크 경로는 internal 만 사용). DDL `ALTER TABLE` 2줄.

---

## 4. 구현 단계 (5단위)

### Unit 1 — DB 스키마 변경

**파일**: `server/smart_cast_db/models/inspection.py`
**변경**:
```python
class AiInferenceTxn(Base):
    # ... 기존 컬럼 ...
    segmented_image_url = Column(Text, nullable=True)   # 신규
    result_image_url    = Column(Text, nullable=True)   # 신규
```

**DDL 마이그레이션**: `smart_cast_db/scripts/` 에 manual SQL 추가 (또는 alembic 미사용이면 raw SQL 안내문). 운영 환경 마이그레이션 안내는 PR body 에 명시.

```sql
ALTER TABLE smartcast.ai_inference_txn
    ADD COLUMN segmented_image_url TEXT,
    ADD COLUMN result_image_url    TEXT;
```

### Unit 2 — 결과 이미지 디스크 저장

**신규 파일**: `services/command/ai_result_image_sink_command.py`

기능:
- 입력: `item_id`, `insp_txn_id`, `segmented_image_b64`, `result_image_b64`
- base64 디코드 후 `{MGMT_INSP_IMAGE_SAVE_DIR}/{item_id}/{insp_txn_id}_segmented.png`, `_result.png` 로 저장
- 반환: `(segmented_url, result_url)` — HttpImageServer 외부 URL

URL 합성: `{MGMT_IMAGE_BASE_URL}/inspections/{item_id}/{filename}`
- `MGMT_IMAGE_BASE_URL` 신규 env. default: `http://{MGMT_IMAGE_BASE_HOST}:{MGMT_IMAGE_HTTP_PORT}` (예: `http://127.0.0.1:18800`)

### Unit 3 — AIAdapter / record_inspection_result wire

**파일**: `services/core/adapters/ai_adapter.py`, `services/command/inspection_result_command.py`

- AIAdapter.execute success path 에서 `AiResultImageSinkCommand.save()` 호출 → `(seg_url, res_url)` 획득
- `_inference_to_dict` 에 `segmented_image_url`, `result_image_url` 키 추가
- `record_inspection_result` 에 `segmented_image_url`, `result_image_url` kwargs 추가 → `AiInferenceTxn` INSERT 시 채움

실패 path (base64 디코드 실패 / 디스크 쓰기 실패): inference 자체는 SUCC 로 두되 URL = NULL. 로그 warning.

### Unit 4 — quality.list_inspections 응답에 image_url 노출

**파일**: `interface_service/app/routes/quality.py`, `app/schemas/schemas.py`

- `InspTaskTxnOut` 에 `segmented_image_url: str | None`, `result_image_url: str | None` 추가
- `list_inspections` 쿼리에 `AiInferenceTxn` outerjoin (이미 `InspStat.patchcore_inference_id` 로 연결) → URL 채움
- `image_id` 별도 필드 미사용 — PyQt 가 `image_url` 직접 fetch

### Unit 5 — PyQt vision_feed 갱신

**파일**: `ui/pyqt/factory_operator/.../widgets/vision_feed.py`, `app/pages/quality.py`, `app/clients/quality.py`

- `load_image_for(image_url)` 로 signature 변경 (또는 `load_image_url(url)` 신규 메서드)
- `_IMAGE_BASE_URL` 의존 제거 — backend 가 완전 URL 제공
- `clients/quality.py` 의 `get_quality_inspections` 가 `result_image_url` 추출
- `pages/quality.py:192,246` 의 `load_image_for(target.get("image_id"))` → `load_image_url(target.get("result_image_url"))`

기존 `image_id` 흐름은 mock 데이터 한정으로 유지하거나 제거. mock_data.py 갱신 필요.

### Unit 6 — 테스트

| 테스트 | 종류 | 위치 |
|---|---|---|
| `AiResultImageSinkCommand.save` base64 → 파일 저장 | unit | `tests/unit/test_ai_result_image_sink.py` (신규) |
| AIAdapter.execute 가 sink command 호출 + URL 전달 | unit | `tests/unit/test_ai_adapter_image_sink.py` 또는 기존 확장 |
| `record_inspection_result` 가 신규 컬럼 INSERT | unit | `tests/integration/test_inspection_result_command.py` (DB 필요, optional) |
| `quality.list_inspections` 응답에 URL 노출 | unit | `routes/tests/test_dashboard_quality_debug_routes.py` 확장 |
| e2e: AI mock → backend 저장 → URL → fetch | integration | `test_ai_adapter_flow.py` 확장 (mock /predict 응답 base64 활용) |

---

## 5. PR / 커밋 분리

PR 1개로 묶되 커밋은 4개로 분할:

1. `feat(db): ai_inference_txn 에 segmented_image_url/result_image_url 컬럼 추가`
2. `feat(ai): AI 결과 이미지 디스크 저장 + AiInferenceTxn 영속화`
3. `feat(quality): list_inspections 응답에 result_image_url 노출`
4. `feat(pyqt): vision_feed 가 backend image_url 로 fetch`

PR 분리 후보 (메모리 [PR 분리 정책: backend ↔ device 분리]):
- backend (1~3) + PyQt (4) 분리 — 그러나 둘 다 backend/UI 라 단일 PR 도 무방. **결정**: 단일 PR.

---

## 6. 위험·고려사항

| 항목 | 평가 | 완화 |
|---|---|---|
| DDL 마이그레이션 누락 | 운영 환경에서 INSERT 실패 | PR body 에 SQL 명시 + 로컬 dev DB 적용 절차 안내 |
| AI mock 의 1x1 dummy PNG → 실 PyQt 표시 시 너무 작음 | 화면에 점만 표시 | mock 갱신 별도 트랙. integration 테스트는 schema 정합만 검증 |
| HttpImageServer 가 dev 환경에서 안 떠 있을 가능성 | 이미지 fetch 실패 → placeholder | `container.start` 에서 자동 기동, 본 PR 변경 없음 |
| `MGMT_INSP_IMAGE_SAVE_DIR` 권한 (default `/var/lib/casting/...`) | 디스크 쓰기 실패 | 로컬 dev 는 env override 안내 |
| PyQt `_IMAGE_BASE_URL` 가 AI 서버 8080 default → 본 PR 로 backend 18800 으로 전환 | 기존 mock 흐름과 일관성 깨질 수 있음 | mock_data 의 image_id 도 URL 로 변경 |

---

## 7. Acceptance Criteria

- [ ] `AiInferenceTxn.segmented_image_url`, `result_image_url` 컬럼 추가 + 모델 반영
- [ ] AIAdapter.execute 성공 path 에서 base64 디코드 → 디스크 저장 → URL 영속화
- [ ] `quality.list_inspections` 응답에 `result_image_url` 포함
- [ ] PyQt vision_feed 가 `result_image_url` 로 HTTP GET → 화면 표시
- [ ] mock `/predict` 의 1x1 dummy PNG 가 placeholder 가 아닌 실제 (작지만) 이미지로 노출 검증
- [ ] 기존 `test_ai_adapter_flow.py` 5건 + 신규 unit 테스트 통과
- [ ] dev 머지 후 baseline 실패는 본 PR 로 늘어나지 않음

---

## 8. 사용자 확정 필요 사항

본 PLAN 진행 전 다음 4 결정 confirmation 부탁드립니다 (위 §3 의 권장안 그대로 가도 OK):

1. **D1 호스팅**: backend HttpImageServer 채택?
2. **D2 URL 패턴**: 2-segment + 응답 image_url 완전 URL 채택?
3. **D3 표시**: result_image 1장만 채택?
4. **D4 DB 위치**: AiInferenceTxn 컬럼 2개 추가 채택?
