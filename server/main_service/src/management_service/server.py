"""Management Service gRPC 서버 (port 50051).

공장 가동 SPOF 제거 목적으로 FastAPI(Interface Service)와 독립 프로세스로 실행된다.
Factory Operator PC(PyQt)가 직접 gRPC 로 호출하며, Interface Service 장애/이관 시에도
공장 운영이 중단되지 않는다.

실행:
    cd backend/management
    python -m grpc_tools.protoc -I proto --python_out=. --grpc_python_out=. proto/management.proto
    python server.py

환경 변수:
    MANAGEMENT_GRPC_HOST  기본 0.0.0.0
    MANAGEMENT_GRPC_PORT  기본 50051
    MANAGEMENT_DB_URL     SQLAlchemy URL (Interface Service 와 동일 DB 공유)

@MX:ANCHOR: V6 Phase 1~8 의 단일 진입점. ManagementServicer + ImagePublisherServicer 등록.
        모든 PyQt gRPC 호출이 본 서버를 통과 — 변경 시 호환성 영향 큼.
@MX:REASON: 5개 service 모듈 + 2개 servicer 의 wiring 책임. RPC 추가 시 servicer 메서드 + proto 동시 갱신 필요.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
from concurrent import futures
from pathlib import Path

import grpc

# `python server.py` 로 직접 실행해도 management/service + server 루트를 모두 찾게 한다.
_THIS_DIR = Path(__file__).resolve().parent
_SRC_DIR = _THIS_DIR.parent
_SERVER_ROOT = next(
    (parent for parent in _THIS_DIR.parents if (parent / "smart_cast_db").exists()),
    None,
)
for path in (_THIS_DIR, _SRC_DIR, _SERVER_ROOT):
    if path is not None and str(path) not in sys.path:
        sys.path.insert(0, str(path))

import management_pb2  # type: ignore  # noqa: E402
import management_pb2_grpc  # type: ignore  # noqa: E402
from rpc.field_event_rpc import FieldEventRpcMixin  # noqa: E402
from rpc.hardware_rpc import HardwareRpcMixin, ImagePublisherServicer  # noqa: E402
from rpc.logistics_rpc import LogisticsRpcMixin  # noqa: E402
from rpc.monitor_rpc import MonitorRpcMixin  # noqa: E402
from rpc.operations_rpc import OperationsRpcMixin  # noqa: E402
from rpc.pattern_rpc import PatternRpcMixin  # noqa: E402
from rpc.production_rpc import ProductionRpcMixin  # noqa: E402
from rpc.production_telemetry_rpc import ProductionTelemetryRpcMixin  # noqa: E402
from rpc.quality_rpc import QualityRpcMixin  # noqa: E402
from rpc.robot_rpc import RobotRpcMixin  # noqa: E402
from rpc.task_rpc import TaskRpcMixin  # noqa: E402
from rpc.traffic_rpc import TrafficRpcMixin  # noqa: E402
from rpc.user_rpc import UserRpcMixin  # noqa: E402
from container import container  # noqa: E402
from event_gateway_servicer import add_to_grpc_server as _add_event_gateway  # noqa: E402

logger = logging.getLogger(__name__)

HOST = os.environ.get("MANAGEMENT_GRPC_HOST", "0.0.0.0")
PORT = int(os.environ.get("MANAGEMENT_GRPC_PORT", "50051"))


class OrchestratorThread:
    """Async<->sync bridge for running Orchestrator coroutines from gRPC threads."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self.thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="OrchestratorThread",
        )
        self.thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("Orchestrator event loop failed to start.")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        logger.info("Orchestrator Thread & Event Loop Started.")
        try:
            self.loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self.loop.close()
            logger.info("Orchestrator Thread & Event Loop Stopped.")

    def run_coro(self, coro):
        return self.submit_coro(coro).result(timeout=5.0)

    def submit_coro(self, coro):
        if self.loop.is_closed():
            raise RuntimeError("Orchestrator 루프가 꺼져있습니다.")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        if self.loop.is_closed():
            return
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5.0)


class ManagementServicer(
    ProductionRpcMixin,
    ProductionTelemetryRpcMixin,
    PatternRpcMixin,
    TaskRpcMixin,
    TrafficRpcMixin,
    MonitorRpcMixin,
    LogisticsRpcMixin,
    OperationsRpcMixin,
    QualityRpcMixin,
    RobotRpcMixin,
    FieldEventRpcMixin,
    HardwareRpcMixin,
    UserRpcMixin,
    management_pb2_grpc.ManagementServiceServicer,
):
    """gRPC servicer wiring for Management RPC mixins."""

    def __init__(self) -> None:
        # Container에서 조립 완료된 의존성 객체들을 참조
        self.orchestrator = container.orchestrator

        self.task_manager = container.task_manager
        self.task_allocator = container.task_allocator
        self.task_executor = container.task_executor

        self.traffic_manager = container.traffic_manager


        self.pattern_command_service = container.pattern_command_service
        self.manual_inspection_command_service = container.manual_inspection_command_service
        self.handoff_command_service = container.handoff_command_service
        self.item_query_service = container.item_query_service
        self.logistics_query_service = container.logistics_query_service
        self.operations_query_service = container.operations_query_service
        self.pattern_query_service = container.pattern_query_service
        self.production_order_query_service = container.production_order_query_service
        self.quality_query_service = container.quality_query_service
        self.schedule_query_service = container.schedule_query_service
        self.event_bridge = container.event_bridge
        self.state_manager = container.state_manager

        self.execution_monitor = container.execution_monitor

        self.amr_battery = container.amr_battery
        self.orchestrator_thread: OrchestratorThread | None = None

    def Health(self, request, context):
        return management_pb2.Empty()


def _load_tls_credentials():
    """V6 S-001: mTLS 환경변수 활성 시 ssl_server_credentials 반환, 아니면 None.

    환경변수:
        MGMT_GRPC_TLS_ENABLED  = 1 면 활성
        MGMT_TLS_CERT_DIR      = cert 디렉터리 (기본 ./certs/)
        MGMT_TLS_SERVER_KEY    = 서버 private key 경로 (기본 ${CERT_DIR}/server.key)
        MGMT_TLS_SERVER_CRT    = 서버 cert 경로 (기본 ${CERT_DIR}/server.crt)
        MGMT_TLS_CA_CRT        = CA cert (클라이언트 검증용, 기본 ${CERT_DIR}/ca.crt)
        MGMT_TLS_REQUIRE_CLIENT_CERT = 1 면 mTLS, 0 이면 server-only TLS (기본 1)
    """
    if os.environ.get("MGMT_GRPC_TLS_ENABLED", "0") not in ("1", "true", "yes"):
        return None
    cert_dir = os.environ.get(
        "MGMT_TLS_CERT_DIR",
        os.path.join(_THIS_DIR, "certs"),
    )
    key_path = os.environ.get("MGMT_TLS_SERVER_KEY", os.path.join(cert_dir, "server.key"))
    crt_path = os.environ.get("MGMT_TLS_SERVER_CRT", os.path.join(cert_dir, "server.crt"))
    ca_path = os.environ.get("MGMT_TLS_CA_CRT", os.path.join(cert_dir, "ca.crt"))

    for p in (key_path, crt_path, ca_path):
        if not os.path.exists(p):
            raise FileNotFoundError(
                f"mTLS 활성화됐으나 cert 파일 없음: {p}\nscripts/gen_certs.sh 를 먼저 실행하세요."
            )

    with open(key_path, "rb") as f:
        server_key = f.read()
    with open(crt_path, "rb") as f:
        server_crt = f.read()
    with open(ca_path, "rb") as f:
        ca_crt = f.read()

    require_client = os.environ.get("MGMT_TLS_REQUIRE_CLIENT_CERT", "1") in ("1", "true", "yes")
    creds = grpc.ssl_server_credentials(
        [(server_key, server_crt)],
        root_certificates=ca_crt,
        require_client_auth=require_client,
    )
    logger.info(
        "mTLS 활성: cert_dir=%s require_client_auth=%s",
        cert_dir,
        require_client,
    )
    return creds

def serve() -> None:
    # WatchItems + WatchAlerts + WatchCameraFrames 등 스트리밍이 워커 점유.
    # 다중 PyQt 클라이언트 + 내부 모니터링 대비 여유있게 32로 확장.
    # keepalive: 죽은 클라이언트(단절된 TCP) 를 빠르게 정리. 구독자 수 가볍게 유지.
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=32),
        options=[
            ("grpc.keepalive_time_ms", 30000),
            ("grpc.keepalive_timeout_ms", 10000),
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.min_ping_interval_without_data_ms", 20000),
            ("grpc.http2.max_pings_without_data", 0),
        ],
    )
    servicer = ManagementServicer()
    management_pb2_grpc.add_ManagementServiceServicer_to_server(servicer, server)
    management_pb2_grpc.add_ImagePublisherServiceServicer_to_server(
        ImagePublisherServicer(), server
    )
    # EventGateway servicer — sole external wire for hardware/UI ↔ EventBridge.
    # Jetson(jetson_publisher) + PyQt(monitoring) 가 PublishEvent / WatchEvents 만 사용.
    _add_event_gateway(server, container.event_bridge)
    bind_addr = f"{HOST}:{PORT}"

    creds = _load_tls_credentials()
    if creds is not None:
        server.add_secure_port(bind_addr, creds)
        scheme = "TLS"
    else:
        server.add_insecure_port(bind_addr)
        scheme = "INSECURE (V6 S-001: MGMT_GRPC_TLS_ENABLED=1 권장)"

    orchestrator_thread = OrchestratorThread()
    servicer.orchestrator_thread = orchestrator_thread
    # sync 스레드(gRPC servicer)가 async handler 코루틴을 던질 루프 주입.
    container.event_bridge.set_loop(orchestrator_thread.loop)
    container.amr_state_monitor.set_async_submitter(orchestrator_thread.submit_coro)

    container.start()
    server.start()
    logger.info("Management Service listening on %s [%s]", bind_addr, scheme)

    # Graceful shutdown
    def _stop(_signum, _frame):
        logger.info("Shutting down...")
        container.close()
        orchestrator_thread.stop()
        server.stop(grace=5)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    server.wait_for_termination()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    serve()
