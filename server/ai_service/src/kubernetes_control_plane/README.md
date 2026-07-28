# Kubernetes Control Plane

AI 파이프라인 전체를 Kubernetes로 배포하기 위한 매니페스트 모음입니다.

## 구성 개요

```text
외부 클라이언트
    │ NodePort 30000
    ▼
[router]              ← ai-server-1
    ├─ /crop  ──────> [preprocessing]   ← ai-server-2
    └─ /predict ────> [model-circle]    ← ai-server-1
                      [model-ellipse]   ← ai-server-2
                      [model-rectangle] ← ai-server-3
```

| Pod | 노드 | 이미지 |
| --- | --- | --- |
| router | ai-server-1 | addteam2/routing-server:v2.2 |
| model-circle | ai-server-1 | addteam2/ai-server:v2.6 |
| preprocessing | ai-server-2 | addteam2/preprocessing-server:v1.0 |
| model-ellipse | ai-server-2 | addteam2/ai-server:v2.6 |
| model-rectangle | ai-server-3 | addteam2/ai-server:v2.6 |

## 사전 준비

### 1. DockerHub Secret 생성

```bash
kubectl create secret docker-registry dockerhub-secret \
  --docker-username=<username> \
  --docker-password=<password>
```

### 2. 모델 체크포인트

각 모델 Pod가 배치되는 노드에 체크포인트 파일을 준비합니다.  
Pod는 호스트의 `/opt/checkpoints`를 마운트하므로, 해당 경로에 파일을 위치시킵니다.

| 노드 | 경로 | Pod | ckpt |
| --- | --- | --- | --- |
| ai-server-1 | `/opt/checkpoints/model.ckpt` | model-circle | [download](https://drive.google.com/file/d/1u0seFNLTVm1yG6Nm9ZYjHlGvuzsCbmNc/view?usp=drive_link) |
| ai-server-2 | `/opt/checkpoints/model.ckpt` | model-ellipse | [download](https://drive.google.com/file/d/1eDZBFZD3rrGtlCwvqzCu5SYb6DYURcGL/view?usp=drive_link) |
| ai-server-3 | `/opt/checkpoints/model.ckpt` | model-rectangle | [download](https://drive.google.com/file/d/1sXuC_MpSS8dTvSyg9DxUKonOXXhpTiS0/view?usp=drive_link) |

SAM2 체크포인트([download](https://drive.google.com/file/d/1p9pES_OpBUTn0mj2_CCZm9lQfDCFZ1h9/view?usp=drive_link))는 `preprocessing_server/checkpoints` 경로에 파일을 위치시킵니다.

### 3. 노드 hostname 확인

매니페스트의 `nodeSelector`는 `kubernetes.io/hostname` 레이블을 사용합니다.  
노드 레이블을 확인하려면:

```bash
kubectl get nodes --show-labels
```

## 배포

```bash
kubectl apply -f .
```

## 네트워크 구성

| 서비스 | 타입 | 포트 | 접근 범위 |
| --- | --- | --- | --- |
| router-service | NodePort | 30000 | 외부 접근 가능 |
| preprocessing-service | ClusterIP | 8080 | 클러스터 내부 |
| model-circle-service | ClusterIP | 8080 | 클러스터 내부 |
| model-ellipse-service | ClusterIP | 8080 | 클러스터 내부 |
| model-rectangle-service | ClusterIP | 8080 | 클러스터 내부 |

외부에서의 요청은 `http://<node-ip>:30000/predict`로 전송합니다. node-ip는 `tailscale ip` 사용

## 상태 확인

```bash
# Pod 상태 확인
kubectl get pods

# 서비스 확인
kubectl get svc

# 특정 Pod 로그 확인
kubectl logs -f deployment/router
kubectl logs -f deployment/preprocessing
kubectl logs -f deployment/model-circle
```
