# Routing Server

외부 요청을 받아 preprocessing → model 서버로 2단계 파이프라인을 조율하는 라우터 서버입니다.

## Docker Image

```bash
docker pull addteam2/routing-server:v2.2
```

## 환경변수

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `PREPROCESSING_URL` | `http://preprocessing-service:8080` | 전처리 서버 주소 |
| `CIRCLE_URL` | `http://model-circle-service:8080` | circle 모델 서버 주소 |
| `ELLIPSE_URL` | `http://model-ellipse-service:8080` | ellipse 모델 서버 주소 |
| `RECTANGLE_URL` | `http://model-rectangle-service:8080` | rectangle 모델 서버 주소 |

## 배포

Kubernetes를 통해 배포됩니다. [kubernetes_control_plane/README.md](../kubernetes_control_plane/README.md) 참고

## API

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| `POST` | `/predict` | 이미지 이상 탐지 파이프라인 실행 |
| `GET` | `/healthz` | 서버 헬스체크 |
| `GET` | `/readyz` | 라우팅 설정 확인 |

### POST /predict

- **요청**: `multipart/form-data`
  - `file`: 이미지 파일 (png/jpg)
  - `model`: 모델 코드 (`CMH` / `EMH` / `RMH`)

| 모델 코드 | 제품 형태 |
| --- | --- |
| `CMH` | circle |
| `EMH` | ellipse |
| `RMH` | rectangle |

- **처리 흐름**:
  1. preprocessing-server `/crop` 호출 → 객체 crop
  2. 해당 model-server `/predict` 호출 → 이상 탐지 결과 반환
