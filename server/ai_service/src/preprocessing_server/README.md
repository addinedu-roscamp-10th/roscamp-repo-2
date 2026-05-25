# Preprocessing Server (GroundedSAM2)

GroundingDINO + SAM2를 사용해 이미지에서 제품 객체를 감지하고 crop하는 전처리 서버입니다.  
routing_server의 요청을 받아 동작합니다.

[Grounded-SAM-2](https://github.com/IDEA-Research/Grounded-SAM-2) 원본 레포지토리를 기반으로,  
`segmenter.py`와 `segmenter_server.py`를 추가해 FastAPI 서버로 구성했습니다.

## Docker Image

```bash
docker pull addteam2/preprocessing-server:v1.1
```

## 환경변수

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `DEVICE` | `cuda` (GPU 없으면 `cpu`) | 추론 장치 |
| `GROUNDING_MODEL` | `IDEA-Research/grounding-dino-tiny` | GroundingDINO 모델 ID |
| `SAM2_CHECKPOINT` | `./checkpoints/sam2.1_hiera_large.pt` | SAM2 체크포인트 경로 |
| `SAM2_CONFIG` | `configs/sam2.1/sam2.1_hiera_l.yaml` | SAM2 설정 파일 경로 |

## 배포

Kubernetes를 통해 배포됩니다. [kubernetes_control_plane/README.md](../kubernetes_control_plane/README.md) 참고

## API

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| `POST` | `/crop` | 객체 감지 후 crop 결과를 base64 JSON으로 반환 (router용) |
| `POST` | `/segment` | crop 결과 PNG 직접 반환 (디버그용) |
| `GET` | `/healthz` | 서버 헬스체크 |
| `GET` | `/readyz` | 모델 로딩 완료 여부 |

### POST /crop

- **요청**: `multipart/form-data`
  - `file`: 이미지 파일 (png/jpg)
  - `product`: 제품 형태 (`circle` / `ellipse` / `rectangle`)
  - `text_prompt`: GroundingDINO 프롬프트 (router가 product별로 결정)
- **응답**:

```json
{
  "cropped_image": "<base64 PNG>"
}
```

- **처리 흐름**: GroundingDINO(text → bbox) → SAM2(bbox → mask) → RGBA crop 반환
