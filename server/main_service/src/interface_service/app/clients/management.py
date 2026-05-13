"""Management Service gRPC client — Interface Service → Management 화살표 실체화.

V6 canonical 아키텍처 Phase 4 (Phase C-1): Interface Service(FastAPI, :8000) 가
Management Service(gRPC, :50051) 를 호출할 때 사용하는 싱글톤 stub.

현재 범위:
  - Health RPC proxy
  - StartProduction write proxy
  - Graceful degradation: Management 미가동 시 ManagementUnavailable 예외 → 503 응답

환경변수:
  MANAGEMENT_GRPC_HOST     기본 localhost
  MANAGEMENT_GRPC_PORT     기본 50051
  MANAGEMENT_GRPC_TIMEOUT  기본 3.0 (초)
  MGMT_GRPC_TLS_ENABLED    1 이면 mTLS (Management 서버와 동일 cert 사용)
"""

from __future__ import annotations

import logging
import os
import sys
import threading

import grpc

logger = logging.getLogger("app.clients.management")

# Management 의 proto 산출물은 src/management_service/ 에 *_pb2.py 형태로 위치.
# 2026-05-13: 이전 코드는 src/main_service/management 를 가리켜 proto import 가 항상 실패하고
#             ManagementUnavailable("proto stubs not compiled") 으로 폴백하던 버그를 수정.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
_MGMT_DIR = os.path.join(_SRC_DIR, "management_service")
if _MGMT_DIR not in sys.path:
    sys.path.insert(0, _MGMT_DIR)

try:
    import management_pb2  # type: ignore
    import management_pb2_grpc  # type: ignore

    _PROTO_AVAILABLE = True
except ImportError as exc:
    # 2026-05-14: WARNING → ERROR 승격.
    # proto stubs 로딩 실패는 Management 미가동(런타임 장애)이 아니라 빌드/배포 구성 오류다.
    # 이전엔 WARNING 으로만 남고 모든 RPC 가 ManagementUnavailable 로 폴백되며,
    # /api/production/start 같이 local fallback 이 있는 라우트는 정상 동작처럼 보여
    # 운영 중에도 발견이 늦어질 수 있었다.
    logger.error(
        "Management proto stubs import FAILED (CONFIG ERROR, not a service outage): %s. "
        "Regenerate via 'python -m grpc_tools.protoc -I rpc/proto --python_out=. --grpc_python_out=. "
        "rpc/proto/management.proto' from server/main_service/src/management_service "
        "or verify sys.path includes the management_service stubs directory.",
        exc,
    )
    management_pb2 = None  # type: ignore
    management_pb2_grpc = None  # type: ignore
    _PROTO_AVAILABLE = False


HOST = os.environ.get("MANAGEMENT_GRPC_HOST", "localhost")
PORT = int(os.environ.get("MANAGEMENT_GRPC_PORT", "50051"))
TIMEOUT = float(os.environ.get("MANAGEMENT_GRPC_TIMEOUT", "3.0"))


class ManagementUnavailable(RuntimeError):
    """Management Service 미가동 / proto stubs 없음 / 타임아웃 등."""


class ManagementClient:
    """Interface Service → Management Service gRPC 싱글톤.

    채널은 lazy 로 생성하고 프로세스 lifetime 동안 재사용.
    장애 시 ManagementUnavailable 을 던져 FastAPI route 에서 503 으로 매핑.
    """

    _instance: ManagementClient | None = None
    _lock = threading.Lock()

    def __init__(self, host: str = HOST, port: int = PORT, timeout: float = TIMEOUT) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._channel: grpc.Channel | None = None
        self._stub = None  # ManagementServiceStub lazy 생성

    @classmethod
    def get(cls) -> ManagementClient:
        """프로세스 전역 싱글톤."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @property
    def endpoint(self) -> str:
        return f"{self._host}:{self._port}"

    def _ensure_channel(self) -> None:
        if not _PROTO_AVAILABLE:
            raise ManagementUnavailable(
                "proto stubs not compiled — run 'make proto' in backend/management"
            )
        if self._channel is None:
            target = self.endpoint
            if os.environ.get("MGMT_GRPC_TLS_ENABLED", "0") in ("1", "true", "yes"):
                creds = self._load_tls_credentials()
                self._channel = grpc.secure_channel(target, creds)
                logger.info("Management gRPC 채널 생성 (TLS): %s", target)
            else:
                self._channel = grpc.insecure_channel(target)
                logger.info("Management gRPC 채널 생성 (insecure): %s", target)
            self._stub = management_pb2_grpc.ManagementServiceStub(self._channel)

    @staticmethod
    def _load_tls_credentials() -> grpc.ChannelCredentials:
        cert_dir = os.environ.get("MGMT_TLS_CERT_DIR", os.path.join(_MGMT_DIR, "certs"))
        ca_path = os.environ.get("MGMT_TLS_CA_CRT", os.path.join(cert_dir, "ca.crt"))
        client_key_path = os.environ.get(
            "MGMT_TLS_CLIENT_KEY", os.path.join(cert_dir, "client.key")
        )
        client_crt_path = os.environ.get(
            "MGMT_TLS_CLIENT_CRT", os.path.join(cert_dir, "client.crt")
        )
        with open(ca_path, "rb") as f:
            ca = f.read()
        key = crt = None
        if os.path.exists(client_key_path) and os.path.exists(client_crt_path):
            with open(client_key_path, "rb") as f:
                key = f.read()
            with open(client_crt_path, "rb") as f:
                crt = f.read()
        return grpc.ssl_channel_credentials(
            root_certificates=ca, private_key=key, certificate_chain=crt
        )

    def health(self) -> None:
        """Management Health RPC 호출. 정상=반환, 장애=ManagementUnavailable."""
        try:
            self._ensure_channel()
            assert self._stub is not None
            self._stub.Health(management_pb2.Empty(), timeout=self._timeout)
        except grpc.RpcError as exc:
            raise ManagementUnavailable(
                f"Management {self.endpoint} unreachable: {exc.code()}"
            ) from exc

    def start_production(self, ord_id: int):
        """SPEC-C2 Iteration 3: smartcast v2 단건 생산 개시 proxy.

        Returns: proto StartProductionResult (ord_id, item_id, equip_task_txn_id, message)
        Raises:
            ValueError — ord_id 무효 / 존재하지 않음 / 패턴 미등록 (gRPC INVALID_ARGUMENT)
            ManagementUnavailable — Mgmt 미가동 또는 timeout
        """
        try:
            self._ensure_channel()
            assert self._stub is not None
            resp = self._stub.StartProduction(
                management_pb2.StartProductionRequest(ord_id=int(ord_id)),
                timeout=self._timeout,
            )
        except grpc.RpcError as exc:
            code = exc.code() if hasattr(exc, "code") else None
            if code == grpc.StatusCode.INVALID_ARGUMENT:
                raise ValueError(exc.details() or "invalid argument") from exc
            raise ManagementUnavailable(
                f"StartProduction failed ({code}): {exc.details() if hasattr(exc, 'details') else exc}"
            ) from exc
        # Legacy result field may be empty while canonical ack is populated.
        result = getattr(resp, "result", None)
        if (
            result is not None
            and (
                getattr(result, "ord_id", 0)
                or getattr(result, "item_id", 0)
                or getattr(result, "equip_task_txn_id", 0)
                or getattr(result, "message", "")
            )
        ):
            return result

        ack = getattr(resp, "ack", None)
        if ack is not None and getattr(ack, "orders", None):
            first = ack.orders[0]

            class _Result:
                ord_id = first.ord_id
                item_id = first.item_id
                equip_task_txn_id = first.equip_task_txn_id
                message = first.reason or ack.message or ""

            return _Result()

        raise ManagementUnavailable("StartProduction returned no result payload")

    def start_shipping(self, ord_id: int) -> dict:
        """출하 트리거 — Management.StartShipping RPC proxy.

        Returns: {"ord_id": int, "item_ids": list[int], "accepted": bool, "message": str}
        Raises:
            ValueError — INVALID_ARGUMENT (예: ord_id <= 0)
            ManagementUnavailable — Mgmt 미가동, timeout, INTERNAL 에러
        """
        try:
            self._ensure_channel()
            assert self._stub is not None
            resp = self._stub.StartShipping(
                management_pb2.StartShippingRequest(ord_id=int(ord_id)),
                timeout=self._timeout,
            )
        except grpc.RpcError as exc:
            code = exc.code() if hasattr(exc, "code") else None
            if code == grpc.StatusCode.INVALID_ARGUMENT:
                raise ValueError(exc.details() or "invalid argument") from exc
            raise ManagementUnavailable(
                f"StartShipping failed ({code}): {exc.details() if hasattr(exc, 'details') else exc}"
            ) from exc
        return {
            "ord_id": int(resp.ord_id),
            "item_ids": [int(x) for x in resp.item_ids],
            "accepted": bool(resp.accepted),
            "message": resp.message or "",
        }

    def register_pattern(self, ord_id: int, ptn_loc_id: int):
        """발주↔패턴 위치 등록 proxy."""
        try:
            self._ensure_channel()
            assert self._stub is not None
            resp = self._stub.RegisterPattern(
                management_pb2.RegisterPatternRequest(
                    ord_id=int(ord_id),
                    ptn_loc_id=int(ptn_loc_id),
                ),
                timeout=self._timeout,
            )
        except grpc.RpcError as exc:
            code = exc.code() if hasattr(exc, "code") else None
            if code == grpc.StatusCode.INVALID_ARGUMENT:
                raise ValueError(exc.details() or "invalid argument") from exc
            if code == grpc.StatusCode.NOT_FOUND:
                raise LookupError(exc.details() or "not found") from exc
            raise ManagementUnavailable(
                f"RegisterPattern failed ({code}): {exc.details() if hasattr(exc, 'details') else exc}"
            ) from exc
        return {
            "ord_id": resp.ord_id,
            "pattern_id": resp.pattern_id if resp.HasField("pattern_id") else None,
            "ptn_loc_id": resp.ptn_loc_id if resp.HasField("ptn_loc_id") else None,
        }

    def close(self) -> None:
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:  # noqa: BLE001
                pass
            self._channel = None
            self._stub = None


def get_management_client() -> ManagementClient:
    """FastAPI dependency-injection 용 헬퍼 (Depends(get_management_client))."""
    return ManagementClient.get()
