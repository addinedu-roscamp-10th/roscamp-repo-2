# AI Server (PatchCore Inference)

컨베이어 벨트 위 제품의 이상 여부를 PatchCore 모델로 판정하는 추론 서버입니다.  
제품 형태(circle / ellipse / rectangle)별로 각각 독립된 컨테이너로 배포됩니다.

## Docker Image

```bash
docker pull addteam2/ai-server:v2.6
```

## 환경변수

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `MODEL` | `unknown` | 모델 식별자 (`circle` / `ellipse` / `rectangle`) |
| `CHECKPOINT_PATH` | `./checkpoints/model.ckpt` | PatchCore 체크포인트 경로 |
| `DEVICE` | `cuda` (GPU 없으면 `cpu`) | 추론 장치 |
| `IMG_H` | `256` | 입력 이미지 높이 |
| `IMG_W` | `256` | 입력 이미지 너비 |

## 배포

Kubernetes를 통해 배포됩니다. [kubernetes_control_plane/README.md](../kubernetes_control_plane/README.md) 참고

## API

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| `POST` | `/predict` | 이미지 이상 탐지 추론 |
| `GET` | `/healthz` | 서버 헬스체크 |
| `GET` | `/readyz` | 모델 로딩 완료 여부 |

### POST /predict

- **요청**: `multipart/form-data`, `file` 필드에 이미지(png/jpg)
- **응답**:

```json
{
  "pred_label": "Normal" | "Anomalous",
  "pred_score": 0.7234,
  "segmented_image": "<base64 PNG>",
  "result_image": "<base64 PNG>"
}
```

- `result_image`: 원본 / anomaly heatmap / mask contour 3패널 PNG
