# AI Service

Grounded SAM 2로 검사 이미지에서 제품 영역을 추출하고, 제품 형태별 PatchCore 모델로 결함 여부를 판정하는 AI 서비스입니다.

## 문서

- [Routing Server](src/routing_server/README.md): 전처리 서버와 제품 형태별 AI 서버를 연결하는 요청 흐름과 API
- [Preprocessing Server](src/preprocessing_server/README.md): GroundingDINO와 SAM2를 사용한 제품 영역 추출 방법
- [AI Server](src/ai_server/README.md): 제품 형태별 PatchCore 추론 서버의 설정과 API
- [Kubernetes 배포](src/kubernetes_control_plane/README.md): AI 파이프라인 배포에 필요한 준비 사항과 실행 방법
