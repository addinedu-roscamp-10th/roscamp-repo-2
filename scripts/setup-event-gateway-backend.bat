@echo off
REM EventGateway backend 셋업 (Windows) — 동료 backend PC 에서 1회 실행
REM
REM 사용:
REM   scripts\setup-event-gateway-backend.bat
REM
REM 사전:
REM   - Python venv 활성화 (grpcio-tools 포함)
REM   - repo root 에서 실행

setlocal enabledelayedexpansion

set OUT_DIR=server\main_service\generated
set PROTO_FILE=proto\event_gateway.proto

if not exist "%PROTO_FILE%" (
  echo ERROR: proto file not found: %PROTO_FILE%
  echo        repo root 에서 실행했는지 확인하세요.
  exit /b 2
)

python -c "import grpc_tools" 2>nul
if errorlevel 1 (
  echo ERROR: grpc_tools 미설치 — pip install grpcio-tools 필요
  exit /b 3
)

if not exist "%OUT_DIR%" mkdir "%OUT_DIR%"
echo ^>^>^> 컴파일 중: %PROTO_FILE% to %OUT_DIR%\

python -m grpc_tools.protoc ^
  -I proto ^
  --python_out=%OUT_DIR% ^
  --pyi_out=%OUT_DIR% ^
  --grpc_python_out=%OUT_DIR% ^
  %PROTO_FILE%

if not exist "%OUT_DIR%\__init__.py" type nul > "%OUT_DIR%\__init__.py"

echo.
echo ^>^>^> 컴파일 완료:
dir /b %OUT_DIR%\event_gateway_pb2*

echo.
echo ==============================================================================
echo 다음 단계 — 동료 backend 코드 작업
echo ==============================================================================
echo.
echo (1) EventType StrEnum 에 신규 항목 2개 추가
echo     PP_DONE_REQUESTED = "PP_DONE_REQUESTED"
echo     INSP_COMPLETED    = "INSP_COMPLETED"
echo.
echo (2) EventGatewayServicer 구현 — template 참고
echo     docs\event_gateway\event_gateway_servicer.py.template
echo.
echo (3) gRPC server build 시 servicer 등록 (README 참고)
echo.
echo 자세한 가이드: docs\event_gateway\README.md
echo ==============================================================================
endlocal
