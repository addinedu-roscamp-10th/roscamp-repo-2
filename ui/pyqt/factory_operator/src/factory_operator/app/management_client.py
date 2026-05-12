"""Management Service gRPC 클라이언트 (Factory Operator PC 용).

V6 아키텍처 규정:
- PyQt 는 Interface Service(FastAPI :8000) 대신 Management Service(gRPC :50051) 직결
- Interface Service 장애/AWS 이관 시에도 공장 가동 유지
- 기존 api_client.py(HTTP) 는 WebSocket 수신과 비-Mgmt 조회용 (Phase 8 까지 점진 축소)

환경 변수:
    MANAGEMENT_GRPC_HOST   기본 localhost
    MANAGEMENT_GRPC_PORT   기본 50051
    MANAGEMENT_GRPC_TIMEOUT 기본 5.0 (초)

호출은 동기. PyQt GUI 스레드 차단을 피하려면 QThread / QThreadPool 워커에서 호출.

@MX:NOTE: Phase 4 산출물. schedule.py [▶ 생산 시작] 버튼이 본 클라이언트의 start_production 호출.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import grpc

# protoc 산출물 (monitoring/app/generated/, Makefile: monitoring/scripts/gen_proto.sh)
from app.generated import management_pb2, management_pb2_grpc

logger = logging.getLogger(__name__)

HOST = os.environ.get("MANAGEMENT_GRPC_HOST", "localhost")
PORT = int(os.environ.get("MANAGEMENT_GRPC_PORT", "50051"))
TIMEOUT = float(os.environ.get("MANAGEMENT_GRPC_TIMEOUT", "5.0"))


# ---------- 사용자 친화 DTO (proto 타입 직접 노출 회피) ----------


@dataclass
class ProductionStartOrderAck:
    """StartProduction 주문별 결과 1건."""

    ord_id: int
    accepted: bool
    reason: str
    item_id: int
    equip_task_txn_id: int


@dataclass
class PatternAssignment:
    ord_id: int
    pattern_id: int | None
    ptn_loc_id: int | None


_STATUS_CODE = {1: "QUE", 2: "PROC", 3: "SUCC", 4: "FAIL"}
_STAGE_CODE = {
    1: "QUE",
    2: "MM",
    3: "DM",
    4: "TR_PP",
    5: "PP",
    6: "IP",
    7: "TR_LD",
    8: "SH",
}


def _ack_from_proto(proto_ack) -> ProductionStartOrderAck:
    return ProductionStartOrderAck(
        ord_id=proto_ack.ord_id,
        accepted=proto_ack.accepted,
        reason=proto_ack.reason or "",
        item_id=proto_ack.item_id,
        equip_task_txn_id=proto_ack.equip_task_txn_id,
    )


def _legacy_wo_to_ack(proto_wo) -> ProductionStartOrderAck:
    return ProductionStartOrderAck(
        ord_id=int(proto_wo.order_id or 0),
        accepted=True,
        reason=_STATUS_CODE.get(proto_wo.status, "QUE"),
        item_id=proto_wo.id,
        equip_task_txn_id=0,
    )


class ManagementClient:
    """Management Service 호출 래퍼.

    사용 예:
        client = ManagementClient()
        acks = client.start_production(["2026"])
        for ack in acks:
            print(ack.ord_id, ack.accepted, ack.reason)
    """

    def __init__(self, host: str = HOST, port: int = PORT, timeout: float = TIMEOUT) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._channel = self._build_channel(host, port)
        self._stub = management_pb2_grpc.ManagementServiceStub(self._channel)

    @staticmethod
    def _build_channel(host: str, port: int):
        """V6 S-001: TLS 환경변수 활성 시 secure_channel, 아니면 insecure.

        환경변수:
            MGMT_GRPC_TLS_ENABLED  = 1 면 TLS 활성
            MGMT_TLS_CERT_DIR      = cert 디렉터리 (기본 backend/management/certs)
            MGMT_TLS_CA_CRT        = CA cert (기본 ${CERT_DIR}/ca.crt)
            MGMT_TLS_CLIENT_KEY    = 클라이언트 private key (기본 ${CERT_DIR}/client.key)
            MGMT_TLS_CLIENT_CRT    = 클라이언트 cert (기본 ${CERT_DIR}/client.crt)
            MGMT_TLS_SERVER_NAME   = TLS SNI override (기본 host 값)
        """
        target = f"{host}:{port}"
        # keep-alive ping: 서버/네트워크 끊김을 빠르게 감지 (특히 장기 streaming RPC 용)
        keepalive = [
            ("grpc.keepalive_time_ms", 30000),
            ("grpc.keepalive_timeout_ms", 10000),
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.max_pings_without_data", 0),
        ]
        if os.environ.get("MGMT_GRPC_TLS_ENABLED", "0") not in ("1", "true", "yes"):
            return grpc.insecure_channel(target, options=keepalive)

        # mTLS 활성: client cert 로딩
        # 기본 cert 디렉터리: monitoring/ 옆의 backend/management/certs/
        default_cert_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "backend", "management", "certs")
        )
        cert_dir = os.environ.get("MGMT_TLS_CERT_DIR", default_cert_dir)
        ca_path = os.environ.get("MGMT_TLS_CA_CRT", os.path.join(cert_dir, "ca.crt"))
        key_path = os.environ.get("MGMT_TLS_CLIENT_KEY", os.path.join(cert_dir, "client.key"))
        crt_path = os.environ.get("MGMT_TLS_CLIENT_CRT", os.path.join(cert_dir, "client.crt"))

        for p in (ca_path, key_path, crt_path):
            if not os.path.exists(p):
                raise FileNotFoundError(f"TLS 활성화됐으나 cert 파일 없음: {p}")

        with open(ca_path, "rb") as f:
            ca = f.read()
        with open(key_path, "rb") as f:
            client_key = f.read()
        with open(crt_path, "rb") as f:
            client_crt = f.read()

        creds = grpc.ssl_channel_credentials(
            root_certificates=ca,
            private_key=client_key,
            certificate_chain=client_crt,
        )
        options = list(keepalive)
        sni = os.environ.get("MGMT_TLS_SERVER_NAME")
        if sni:
            options.append(("grpc.ssl_target_name_override", sni))
        logger.info("ManagementClient TLS 활성: %s (sni=%s)", target, sni or host)
        return grpc.secure_channel(target, creds, options=options)

    @property
    def endpoint(self) -> str:
        return f"{self._host}:{self._port}"

    def health(self) -> bool:
        """Mgmt Service 연결 가능 여부."""
        try:
            self._stub.Health(management_pb2.Empty(), timeout=2.0)
            return True
        except grpc.RpcError as e:
            logger.warning("Management health check failed: %s", e)
            return False

    def start_production(self, order_ids: list[str]) -> list[ProductionStartOrderAck]:
        """승인 주문들을 생산 개시. 주문별 ack 리스트 반환.

        Raises:
            grpc.RpcError: 서버 에러/연결 실패 등 (호출자가 try/except 처리)
            ValueError: 빈 order_ids (서버에서 INVALID_ARGUMENT 응답)
        """
        if not order_ids:
            raise ValueError("order_ids 가 비어있습니다")
        req = management_pb2.StartProductionRequest(order_ids=order_ids)
        resp = self._stub.StartProduction(req, timeout=self._timeout)
        if resp.HasField("ack") and resp.ack.orders:
            return [_ack_from_proto(order_ack) for order_ack in resp.ack.orders]
        return [_legacy_wo_to_ack(wo) for wo in resp.work_orders]

    def start_production_one(self, ord_id: int) -> ProductionStartOrderAck:
        acks = self.start_production([str(ord_id)])
        if not acks:
            raise ValueError(f"ord_id={ord_id} start_production returned no ack")
        return acks[0]

    def list_items(self, order_id: str | None = None, limit: int = 100):
        """현재 활성 item 목록. proto Item 메시지 그대로 반환."""
        req = management_pb2.ListItemsRequest(order_id=order_id or "", limit=limit)
        resp = self._stub.ListItems(req, timeout=self._timeout)
        return list(resp.items)

    def list_item_views(self, order_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        items = self.list_items(order_id=order_id, limit=limit)
        rows: list[dict[str, Any]] = []
        for item in items:
            rows.append(
                {
                    "item_id": int(item.id),
                    "ord_id": int(item.order_id or 0),
                    "flow_stat": item.flow_stat or None,
                    "zone_nm": item.zone_nm or None,
                    "result": item.result if item.HasField("result") else None,
                    "equip_task_type": None,
                    "trans_task_type": None,
                    "cur_stat": item.cur_stat or self.stage_code_to_label(int(item.cur_stage)),
                    "cur_res": item.curr_res or "",
                    "is_defective": item.is_defective if item.HasField("is_defective") else None,
                    "updated_at": item.mfg_at.iso8601 or None,
                }
            )
        return rows

    def list_production_orders(
        self,
        *,
        status_filters: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        req = management_pb2.ListProductionOrdersRequest(
            status_filters=status_filters or [],
            limit=limit,
        )
        resp = self._stub.ListProductionOrders(req, timeout=self._timeout)
        return [
            {
                "ord_id": row.ord_id,
                "user_id": row.user_id,
                "latest_stat": row.latest_stat or "",
                "company_name": row.company_name or "-",
                "customer_name": row.customer_name or "-",
                "total_amount": row.total_amount or 0,
                "requested_delivery": row.requested_delivery or "",
                "confirmed_delivery": row.confirmed_delivery or "",
                "created_at": row.created_at or "",
            }
            for row in resp.orders
        ]

    def list_patterns(self) -> list[dict[str, Any]]:
        resp = self._stub.ListPatterns(management_pb2.Empty(), timeout=self._timeout)
        return [
            {
                "ord_id": row.ord_id,
                "pattern_id": row.pattern_id if row.HasField("pattern_id") else None,
                "ptn_loc_id": row.ptn_loc_id if row.HasField("ptn_loc_id") else None,
            }
            for row in resp.patterns
        ]

    def register_pattern(self, ord_id: int, ptn_loc_id: int) -> PatternAssignment:
        resp = self._stub.RegisterPattern(
            management_pb2.RegisterPatternRequest(
                ord_id=int(ord_id),
                ptn_loc_id=int(ptn_loc_id),
            ),
            timeout=self._timeout,
        )
        return PatternAssignment(
            ord_id=resp.ord_id,
            pattern_id=resp.pattern_id if resp.HasField("pattern_id") else None,
            ptn_loc_id=resp.ptn_loc_id if resp.HasField("ptn_loc_id") else None,
        )

    def get_pattern(self, ord_id: int) -> dict[str, Any]:
        resp = self._stub.GetPattern(
            management_pb2.GetPatternRequest(ord_id=int(ord_id)),
            timeout=self._timeout,
        )
        return {
            "ord_id": resp.ord_id,
            "pattern_id": resp.pattern_id if resp.HasField("pattern_id") else None,
            "ptn_loc_id": resp.ptn_loc_id if resp.HasField("ptn_loc_id") else None,
        }

    def list_equip_tasks(
        self,
        *,
        res_id: str | None = None,
        item_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        req = management_pb2.ListEquipTasksRequest(
            res_id=res_id or "",
            item_id=int(item_id or 0),
            limit=limit,
        )
        resp = self._stub.ListEquipTasks(req, timeout=self._timeout)
        return [
            {
                "txn_id": row.txn_id,
                "res_id": row.res_id or None,
                "task_type": row.task_type or None,
                "txn_stat": row.txn_stat or None,
                "item_id": row.item_id if row.HasField("item_id") else None,
                "strg_loc_id": row.strg_loc_id if row.HasField("strg_loc_id") else None,
                "ship_loc_id": row.ship_loc_id if row.HasField("ship_loc_id") else None,
                "req_at": row.req_at or None,
                "start_at": row.start_at or None,
                "end_at": row.end_at or None,
            }
            for row in resp.tasks
        ]

    def get_item_pp_requirements(self, item_id: int) -> dict[str, Any]:
        resp = self._stub.GetItemPpRequirements(
            management_pb2.GetItemPpRequirementsRequest(item_id=int(item_id)),
            timeout=self._timeout,
        )
        return {
            "item_id": resp.item_id,
            "ord_id": resp.ord_id,
            "pp_options": [
                {
                    "pp_id": row.pp_id,
                    "pp_nm": row.pp_nm or None,
                    "extra_cost": row.extra_cost,
                }
                for row in resp.pp_options
            ],
            "pp_task_status": [
                {
                    "txn_id": row.txn_id,
                    "ord_id": row.ord_id,
                    "map_id": row.map_id if row.HasField("map_id") else None,
                    "pp_nm": row.pp_nm or None,
                    "item_id": row.item_id if row.HasField("item_id") else None,
                    "operator_id": row.operator_id if row.HasField("operator_id") else None,
                    "txn_stat": row.txn_stat or None,
                    "req_at": row.req_at or None,
                    "start_at": row.start_at or None,
                    "end_at": row.end_at or None,
                }
                for row in resp.pp_task_status
            ],
        }

    def list_equipment(self) -> list[dict[str, Any]]:
        resp = self._stub.ListEquipment(management_pb2.Empty(), timeout=self._timeout)
        return [
            {
                "res_id": row.res_id,
                "res_type": row.res_type or None,
                "model_nm": row.model_nm or None,
                "zone_id": row.zone_id if row.HasField("zone_id") else None,
                "cur_stat": row.cur_stat or None,
                "err_msg": row.err_msg or None,
                "updated_at": row.updated_at or None,
            }
            for row in resp.equipment
        ]

    def list_stages(self) -> list[dict[str, Any]]:
        resp = self._stub.ListStages(management_pb2.Empty(), timeout=self._timeout)
        return [
            {
                "zone_id": row.zone_id,
                "zone_nm": row.zone_nm or "",
                "in_progress_count": row.in_progress_count,
                "name": row.zone_nm or "",
                "status": f"{row.in_progress_count}건 진행중",
                "equipment": "-",
            }
            for row in resp.stages
        ]

    def list_order_item_progress(self) -> list[dict[str, Any]]:
        resp = self._stub.ListOrderItemProgress(management_pb2.Empty(), timeout=self._timeout)
        return [
            {
                "ord_id": row.ord_id,
                "total_items": row.total_items,
                "by_stat": dict(row.by_stat),
            }
            for row in resp.items
        ]

    def list_schedule_jobs(self) -> list[dict[str, Any]]:
        resp = self._stub.ListScheduleJobs(management_pb2.Empty(), timeout=self._timeout)
        return [
            {
                "id": row.id or "",
                "order_id": row.order_id or "",
                "priority_score": row.priority_score,
                "priority_rank": row.priority_rank,
                "assigned_stage": row.assigned_stage or "",
                "status": row.status or "",
                "estimated_completion": row.estimated_completion or "",
                "started_at": row.started_at or "",
                "completed_at": row.completed_at or "",
                "created_at": row.created_at or "",
                "company_name": row.company_name or "-",
                "customer_name": row.customer_name or "-",
                "total_amount": row.total_amount,
                "requested_delivery": row.requested_delivery or "",
                "confirmed_delivery": row.confirmed_delivery or "",
                "order_status": row.order_status or "",
            }
            for row in resp.jobs
        ]

    def calculate_schedule_priority(self, order_ids: list[str]) -> dict[str, Any]:
        req = management_pb2.CalculateSchedulePriorityRequest(order_ids=order_ids)
        resp = self._stub.CalculateSchedulePriority(req, timeout=self._timeout)
        return {
            "results": [
                {
                    "order_id": row.order_id or "",
                    "company_name": row.company_name or "-",
                    "product_summary": row.product_summary or "",
                    "total_quantity": row.total_quantity,
                    "requested_delivery": row.requested_delivery or "",
                    "total_score": row.total_score,
                    "rank": row.rank,
                    "factors": [
                        {
                            "name": factor.name or "",
                            "score": factor.score,
                            "max_score": factor.max_score,
                            "detail": factor.detail or "",
                        }
                        for factor in row.factors
                    ],
                    "recommendation_reason": row.recommendation_reason or "",
                    "delay_risk": row.delay_risk or "",
                    "ready_status": row.ready_status or "",
                    "blocking_reasons": list(row.blocking_reasons),
                    "estimated_days": row.estimated_days,
                }
                for row in resp.results
            ]
        }

    def watch_items(self, order_id: str | None = None) -> Iterator:
        """Server streaming. 무한 루프 — 별도 QThread 에서 소비."""
        req = management_pb2.WatchItemsRequest(order_id=order_id or "")
        return self._stub.WatchItems(req)

    def watch_alerts(self, severity_filter: str | None = None) -> Iterator:
        """alerts 테이블 신규 row 스트림. 별도 QThread 에서 소비."""
        req = management_pb2.WatchAlertsRequest(severity_filter=severity_filter or "")
        return self._stub.WatchAlerts(req)

    def get_robot_status(self) -> list[dict]:
        """AMR/Cobot 실시간 상태 조회. dict list 반환."""
        try:
            req = management_pb2.GetRobotStatusRequest()
            resp = self._stub.GetRobotStatus(req, timeout=self._timeout)
            return [
                {
                    "id": r.id,
                    "type": "amr" if r.type == 1 else "cobot" if r.type == 2 else "unknown",
                    "host": r.host,
                    "status": r.status,
                    "battery": round(r.battery, 1),
                    "voltage": round(r.voltage, 3),
                    "location": r.location,
                    "task_state": r.task_state,
                    "task_id": r.task_id or "",
                    "loaded_item": r.loaded_item or "",
                }
                for r in resp.robots
            ]
        except grpc.RpcError as e:
            logger.warning("GetRobotStatus 실패: %s", e)
            return []

    def calculate_priority(self, ord_ids: list[int]) -> list[dict[str, Any]]:
        """APPR + 패턴 등록된 선택 주문들의 우선순위 계산.

        Management Service 의 CalculateSchedulePriority RPC 를 호출하고
        rank 오름차순(1 = 최우선)으로 정렬된 결과를 반환한다.
        """
        req = management_pb2.CalculateSchedulePriorityRequest(
            order_ids=[str(i) for i in ord_ids]
        )
        resp = self._stub.CalculateSchedulePriority(req, timeout=self._timeout)
        results = [
            {
                "order_id": int(r.order_id),
                "rank": r.rank,
                "total_score": round(r.total_score, 1),
                "product_summary": r.product_summary or "-",
                "requested_delivery": r.requested_delivery or "-",
                "delay_risk": r.delay_risk or "low",
            }
            for r in resp.results
        ]
        results.sort(key=lambda x: x["rank"])
        return results

    @staticmethod
    def stage_code_to_label(code: int) -> str:
        """proto enum 정수 → 코드 문자열 (UI 매핑용)."""
        return _STAGE_CODE.get(code, "UNSPECIFIED")

    def close(self) -> None:
        self._channel.close()
