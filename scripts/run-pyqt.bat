@echo off
REM PyQt Monitoring 실행 (Windows).
setlocal
set ROOT=%~dp0..
cd /d "%ROOT%\ui\pyqt\factory_operator"

if not exist .venv (echo X .venv 없음. scripts\setup.bat 먼저 실행. & exit /b 1)

call .venv\Scripts\activate.bat
set PYTHONPATH=src

if exist .env.local (
    for /f "tokens=1,2 delims==" %%a in (.env.local) do (
        if not "%%a"=="" if not "%%a:~0,1%"=="#" set %%a=%%b
    )
)

if not defined MANAGEMENT_GRPC_HOST set MANAGEMENT_GRPC_HOST=localhost
if not defined MANAGEMENT_GRPC_PORT set MANAGEMENT_GRPC_PORT=50051
echo -^> PyQt Monitoring 시작 (Management gRPC=%MANAGEMENT_GRPC_HOST%:%MANAGEMENT_GRPC_PORT%)
python -m factory_operator.main
endlocal
