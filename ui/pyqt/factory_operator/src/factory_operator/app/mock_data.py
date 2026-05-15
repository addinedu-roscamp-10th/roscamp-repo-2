"""Mock 데이터 — FastAPI 백엔드 장애 / 오프라인 시 fallback.

네트워크 연결이 끊겨 있거나 백엔드가 응답하지 않을 때 화면이 비지 않도록
대체 데이터를 제공한다. ApiClient 가 None 을 반환할 때 사용.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def _ago(minutes: int = 0, hours: int = 0) -> str:
    dt = datetime.now() - timedelta(minutes=minutes, hours=hours)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


DASHBOARD_STATS: dict[str, Any] = {
    "production_goal_rate": 82.5,
    "today_production": 187,
    "completed_today": 165,
    "active_robots": 5,
    "pending_orders": 12,
    "today_alarms": 3,
    "defect_rate": 2.8,
    "equipment_utilization": 78.4,
    "oee": 72.6,
    "active_orders": 8,
}


ALERTS: list[dict[str, Any]] = [
    {
        "id": 1,
        "level": "warning",
        "message": "용해로 #1 온도 임계값 초과 (1450°C)",
        "source": "FRN-001",
        "created_at": _ago(minutes=2),
    },
    {
        "id": 2,
        "level": "error",
        "message": "AMR #3 배터리 부족 (12%) - 충전 필요",
        "source": "AMR-003",
        "created_at": _ago(minutes=8),
    },
    {
        "id": 3,
        "level": "info",
        "message": "조형기 #1 정기 점검 일정 도래 (D-2)",
        "source": "MLD-001",
        "created_at": _ago(minutes=22),
    },
    {
        "id": 4,
        "level": "warning",
        "message": "검사 카메라 #1 초점 재조정 권장",
        "source": "CAM-001",
        "created_at": _ago(hours=1),
    },
    {
        "id": 5,
        "level": "critical",
        "message": "분류기 #1 벨트 마모 감지 - 교체 필요",
        "source": "SRT-001",
        "created_at": _ago(hours=2),
    },
    {
        "id": 6,
        "level": "info",
        "message": "주문 ORD-2026-045 생산 완료",
        "source": "SYSTEM",
        "created_at": _ago(hours=3),
    },
]


PROCESS_STAGES: list[dict[str, Any]] = [
    {
        "id": 1,
        "label": "용해",
        "status": "running",
        "progress": 100,
        "start_time": _ago(hours=2),
        "equipment_id": "FRN-001",
    },
    {
        "id": 2,
        "label": "주형 제작",
        "status": "completed",
        "progress": 100,
        "start_time": _ago(hours=3),
        "equipment_id": "MLD-001",
    },
    {
        "id": 3,
        "label": "주탕",
        "status": "waiting",
        "progress": 0,
        "start_time": "",
        "equipment_id": "ARM-001",
    },
    {
        "id": 4,
        "label": "냉각",
        "status": "running",
        "progress": 81,
        "start_time": _ago(hours=4),
        "equipment_id": "CLZ-001",
    },
    {
        "id": 5,
        "label": "탈형",
        "status": "idle",
        "progress": 0,
        "start_time": "",
        "equipment_id": "ARM-002",
    },
    {
        "id": 6,
        "label": "후처리",
        "status": "running",
        "progress": 67,
        "start_time": _ago(hours=1),
        "equipment_id": "ARM-003",
    },
    {
        "id": 7,
        "label": "검사",
        "status": "running",
        "progress": 95,
        "start_time": _ago(minutes=30),
        "equipment_id": "CAM-001",
    },
    {
        "id": 8,
        "label": "분류",
        "status": "idle",
        "progress": 0,
        "start_time": "",
        "equipment_id": "SRT-001",
    },
]


EQUIPMENT: list[dict[str, Any]] = [
    {
        "id": "FRN-001",
        "name": "용해로 #1",
        "type": "furnace",
        "status": "running",
        "utilization": 92,
        "last_checked": "2026-03-20",
    },
    {
        "id": "FRN-002",
        "name": "용해로 #2",
        "type": "furnace",
        "status": "idle",
        "utilization": 0,
        "last_checked": "2026-03-18",
    },
    {
        "id": "MLD-001",
        "name": "조형기 #1",
        "type": "mold_press",
        "status": "running",
        "utilization": 85,
        "last_checked": "2026-03-22",
    },
    {
        "id": "ARM-001",
        "name": "로봇암 #1 (주탕)",
        "type": "robot_arm",
        "status": "idle",
        "utilization": 45,
        "last_checked": "2026-03-25",
    },
    {
        "id": "ARM-002",
        "name": "로봇암 #2 (탈형)",
        "type": "robot_arm",
        "status": "idle",
        "utilization": 32,
        "last_checked": "2026-03-24",
    },
    {
        "id": "ARM-003",
        "name": "로봇암 #3 (후처리)",
        "type": "robot_arm",
        "status": "running",
        "utilization": 78,
        "last_checked": "2026-03-26",
    },
    {
        "id": "CAM-001",
        "name": "검사 카메라 #1",
        "type": "camera",
        "status": "running",
        "utilization": 95,
        "last_checked": "2026-03-23",
    },
    {
        "id": "CVR-001",
        "name": "컨베이어 #1",
        "type": "conveyor",
        "status": "running",
        "utilization": 88,
        "last_checked": "2026-03-21",
    },
    {
        "id": "SRT-001",
        "name": "분류기 #1",
        "type": "sorter",
        "status": "running",
        "utilization": 72,
        "last_checked": "2026-03-19",
    },
    {
        "id": "AMR-001",
        "name": "AMR #1",
        "type": "amr",
        "status": "running",
        "utilization": 65,
        "last_checked": "2026-03-28",
    },
    {
        "id": "AMR-002",
        "name": "AMR #2",
        "type": "amr",
        "status": "idle",
        "utilization": 20,
        "last_checked": "2026-03-27",
    },
    {
        "id": "AMR-003",
        "name": "AMR #3",
        "type": "amr",
        "status": "charging",
        "utilization": 0,
        "last_checked": "2026-03-29",
    },
]


QUALITY_STATS: dict[str, Any] = {
    "total": 342,
    "ok": 332,
    "ng": 10,
    "defect_rate": 2.9,
}


# 2026-05-14: web mockInspections 30건과 데이터 단일 진실 원천 동기화.
# 매핑: productId→product (KS 규격 카탈로그), result(pass→OK / fail→NG),
#       defectType→defect_type, inspectorId→inspector, defectDetail→note.
_PRODUCT_NAMES_FOR_INSP: dict[str, str] = {
    "PRD-001": "맨홀 뚜껑 KS D-600",
    "PRD-002": "맨홀 뚜껑 KS D-800",
    "PRD-003": "맨홀 뚜껑 KS D-450",
}

# (id, productId, result, defectType, defectDetail, inspectedAt, confidence, castingId)
_INSP_RAW: list[tuple[str, str, str, str, str, str, float, str]] = [
    ("INS-001", "PRD-001", "pass", "",           "",                                "2026-03-30T09:31:00", 98.5, "CST-0298-01"),
    ("INS-002", "PRD-001", "pass", "",           "",                                "2026-03-30T09:32:00", 97.2, "CST-0298-02"),
    ("INS-003", "PRD-001", "fail", "표면 균열",  "뚜껑 외곽부 0.3mm 크랙",          "2026-03-30T09:33:00", 95.8, "CST-0298-03"),
    ("INS-004", "PRD-001", "fail", "표면 결함",  "외관 비전 검사에서 결함 감지",     "2026-03-30T09:34:00", 99.1, "CST-0298-04"),
    ("INS-005", "PRD-001", "pass", "",           "",                                "2026-03-30T09:35:00", 96.7, "CST-0298-05"),
    ("INS-006", "PRD-001", "fail", "기포 불량",  "내부 기포 2개 감지 (직경 1.5mm)", "2026-03-30T09:36:00", 94.3, "CST-0298-06"),
    ("INS-007", "PRD-001", "pass", "",           "",                                "2026-03-30T09:37:00", 98.0, "CST-0298-07"),
    ("INS-008", "PRD-001", "fail", "표면 결함",  "외관 비전 검사에서 결함 감지",     "2026-03-30T09:38:00", 97.5, "CST-0298-08"),
    ("INS-009", "PRD-001", "pass", "",           "",                                "2026-03-30T10:01:00", 98.8, "CST-0301-01"),
    ("INS-010", "PRD-001", "fail", "수축 결함",  "중앙부 수축 3mm 초과",            "2026-03-30T10:02:30", 92.1, "CST-0301-02"),
    ("INS-011", "PRD-001", "pass", "",           "",                                "2026-03-30T10:04:00", 97.9, "CST-0301-03"),
    ("INS-012", "PRD-001", "pass", "",           "",                                "2026-03-30T10:05:30", 99.3, "CST-0301-04"),
    ("INS-013", "PRD-001", "fail", "표면 균열",  "테두리 미세 균열 0.2mm",          "2026-03-30T10:07:00", 96.4, "CST-0301-05"),
    ("INS-014", "PRD-001", "pass", "",           "",                                "2026-03-30T10:08:30", 98.2, "CST-0301-06"),
    ("INS-015", "PRD-001", "pass", "",           "",                                "2026-03-30T10:10:00", 97.0, "CST-0301-07"),
    ("INS-016", "PRD-001", "fail", "주탕 불량",  "미충전 부위 발생",                "2026-03-30T10:11:30", 93.7, "CST-0301-08"),
    ("INS-017", "PRD-001", "pass", "",           "",                                "2026-03-30T10:13:00", 98.6, "CST-0301-09"),
    ("INS-018", "PRD-001", "pass", "",           "",                                "2026-03-30T10:14:30", 99.0, "CST-0301-10"),
    ("INS-019", "PRD-001", "fail", "치수 불량",  "외경 601.5mm (허용 +-0.5mm)",     "2026-03-29T14:20:00", 91.5, "CST-0295-01"),
    ("INS-020", "PRD-001", "pass", "",           "",                                "2026-03-29T14:21:30", 98.3, "CST-0295-02"),
    ("INS-021", "PRD-001", "fail", "기포 불량",  "표면 기포 다수 (5개 이상)",       "2026-03-29T14:23:00", 89.8, "CST-0295-03"),
    ("INS-022", "PRD-001", "pass", "",           "",                                "2026-03-29T14:24:30", 97.6, "CST-0295-04"),
    ("INS-023", "PRD-001", "fail", "냉각 균열",  "급속 냉각 열응력 균열",            "2026-03-29T14:26:00", 93.2, "CST-0295-05"),
    ("INS-024", "PRD-001", "pass", "",           "",                                "2026-03-29T14:27:30", 99.4, "CST-0295-06"),
    ("INS-025", "PRD-001", "fail", "주형 결함",  "주형 파손에 의한 형상 이상",       "2026-03-29T14:29:00", 90.1, "CST-0295-07"),
    ("INS-026", "PRD-001", "pass", "",           "",                                "2026-03-29T14:30:30", 96.9, "CST-0295-08"),
    ("INS-027", "PRD-001", "fail", "표면 균열",  "하부면 균열 0.5mm",                "2026-03-29T14:32:00", 88.7, "CST-0295-09"),
    ("INS-028", "PRD-001", "pass", "",           "",                                "2026-03-29T14:33:30", 98.1, "CST-0295-10"),
    ("INS-029", "PRD-001", "fail", "수축 결함",  "냉각 수축률 기준 초과",            "2026-03-29T15:10:00", 91.9, "CST-0296-01"),
    ("INS-030", "PRD-001", "fail", "기포 불량",  "내부 기포 밀집 구간",              "2026-03-29T15:11:30", 87.3, "CST-0296-02"),
]

INSPECTIONS: list[dict[str, Any]] = [
    {
        "id": ins_id,
        "inspected_at": at,
        "product": _PRODUCT_NAMES_FOR_INSP.get(prod_id, prod_id),
        "result": "OK" if res == "pass" else "NG",
        "defect_type": dtype,
        "inspector": "CAM-001",
        "note": detail,
        "confidence": conf,
        "casting_id": cid,
        # 2026-05-15: web mockInspections.imageId 와 동일 패턴 — INS-001 ↔ IMG-001.
        # backend HttpImageServer 가 /<image_id>.jpg 로 서빙.
        "image_id": ins_id.replace("INS-", "IMG-"),
    }
    for (ins_id, prod_id, res, dtype, detail, at, conf, cid) in _INSP_RAW
]


TRANSPORT_TASKS: list[dict[str, Any]] = [
    {
        "id": "T-0042",
        "type": "운반",
        "priority": "urgent",
        "from": "주조 구역 C",
        "to": "냉각 구역 D",
        "amr": "AMR-001",
        "status": "running",
        "cargo": "주물 12개",
    },
    {
        "id": "T-0043",
        "type": "운반",
        "priority": "high",
        "from": "검사 F",
        "to": "분류 F",
        "amr": "AMR-002",
        "status": "pending",
        "cargo": "검사품 8개",
    },
    {
        "id": "T-0040",
        "type": "충전",
        "priority": "low",
        "from": "대기 장소",
        "to": "충전소",
        "amr": "AMR-003",
        "status": "running",
        "cargo": "-",
    },
    {
        "id": "T-0041",
        "type": "운반",
        "priority": "normal",
        "from": "후처리 E",
        "to": "검사 구역 F",
        "amr": "AMR-001",
        "status": "completed",
        "cargo": "주물 10개",
    },
    {
        "id": "T-0039",
        "type": "운반",
        "priority": "urgent",
        "from": "분류 F",
        "to": "출고장 G",
        "amr": "AMR-002",
        "status": "pending",
        "cargo": "출고분 30개",
    },
    {
        "id": "T-0038",
        "type": "운반",
        "priority": "normal",
        "from": "주형 B",
        "to": "주조 C",
        "amr": "AMR-001",
        "status": "pending",
        "cargo": "주형 6개",
    },
    {
        "id": "T-0037",
        "type": "운반",
        "priority": "low",
        "from": "용해 A",
        "to": "주형 B",
        "amr": "AMR-001",
        "status": "completed",
        "cargo": "원재료 50kg",
    },
]


AMR_STATUS: list[dict[str, Any]] = [
    {
        "id": "AMR-001",
        "status": "running",
        "battery": 78,
        "location": "이송 구역",
        "current_task": "T-0042",
    },
    {
        "id": "AMR-002",
        "status": "idle",
        "battery": 95,
        "location": "대기 장소",
        "current_task": "-",
    },
    {
        "id": "AMR-003",
        "status": "charging",
        "battery": 12,
        "location": "충전소",
        "current_task": "T-0040",
    },
]


# ===== 차트 데이터 (v0.2 신규) =====

WEEKLY_PRODUCTION: list[dict[str, Any]] = [
    {"day": "월", "production": 168, "defect_rate": 2.4},
    {"day": "화", "production": 182, "defect_rate": 2.1},
    {"day": "수", "production": 165, "defect_rate": 3.3},
    {"day": "목", "production": 194, "defect_rate": 1.8},
    {"day": "금", "production": 201, "defect_rate": 2.0},
    {"day": "토", "production": 156, "defect_rate": 2.9},
    {"day": "일", "production": 143, "defect_rate": 3.6},
]


TEMPERATURE_HISTORY: list[dict[str, Any]] = [
    {"minute": i, "temperature": temp, "target": 1450}
    for i, temp in enumerate(
        [
            25,
            180,
            340,
            490,
            640,
            780,
            910,
            1020,
            1120,
            1200,
            1270,
            1330,
            1380,
            1410,
            1430,
            1442,
            1448,
            1451,
            1449,
            1450,
            1451,
            1450,
            1452,
            1449,
            1450,
            1451,
            1450,
            1450,
            1449,
            1451,
        ]
    )
]


HOURLY_PRODUCTION: list[dict[str, Any]] = [
    {"hour": f"{h:02d}:00", "good": g, "bad": b}
    for h, (g, b) in zip(
        range(8, 20),
        [
            (32, 1),
            (41, 2),
            (38, 1),
            (45, 3),
            (52, 2),
            (48, 1),
            (39, 2),
            (44, 1),
            (50, 3),
            (46, 2),
            (42, 1),
            (35, 1),
        ],
        strict=False,
    )
]


# 2026-05-14: web (ui/web/src/lib/mock-data.ts) 의 mockProductionMetrics / mockDefectTypeStats /
# mockInspectionStandards 와 데이터 단일 진실 원천 동기화.
# (date, production, defects, defectRate) — web 30일 값과 1:1 일치.
_PROD_METRICS_30D: list[tuple[str, int, int, float]] = [
    ("03/01", 45, 2, 4.4),
    ("03/02", 0,  0, 0.0),
    ("03/03", 52, 3, 5.8),
    ("03/04", 58, 2, 3.4),
    ("03/05", 61, 4, 6.6),
    ("03/06", 55, 1, 1.8),
    ("03/07", 49, 2, 4.1),
    ("03/08", 43, 3, 7.0),
    ("03/09", 0,  0, 0.0),
    ("03/10", 57, 2, 3.5),
    ("03/11", 63, 5, 7.9),
    ("03/12", 60, 3, 5.0),
    ("03/13", 65, 2, 3.1),
    ("03/14", 58, 1, 1.7),
    ("03/15", 50, 2, 4.0),
    ("03/16", 0,  0, 0.0),
    ("03/17", 54, 3, 5.6),
    ("03/18", 62, 2, 3.2),
    ("03/19", 59, 4, 6.8),
    ("03/20", 66, 2, 3.0),
    ("03/21", 64, 1, 1.6),
    ("03/22", 48, 2, 4.2),
    ("03/23", 0,  0, 0.0),
    ("03/24", 42, 2, 4.8),
    ("03/25", 55, 3, 5.5),
    ("03/26", 48, 1, 2.1),
    ("03/27", 61, 2, 3.3),
    ("03/28", 38, 3, 7.9),
    ("03/29", 52, 2, 3.8),
    ("03/30", 47, 2, 4.3),
]


DEFECT_RATE_TREND: list[dict[str, Any]] = [
    {"label": d, "rate": r} for (d, _p, _b, r) in _PROD_METRICS_30D
]


# web mockDefectTypeStats 와 동일한 7종.
DEFECT_TYPE_DIST: list[dict[str, Any]] = [
    {"type": "표면 균열", "count": 12},
    {"type": "기포 불량", "count": 9},
    {"type": "수축 결함", "count": 7},
    {"type": "치수 불량", "count": 6},
    {"type": "냉각 균열", "count": 4},
    {"type": "주탕 불량", "count": 2},
    {"type": "주형 결함", "count": 2},
]


VISION_FEED: dict[str, Any] = {
    "result": "pass",
    "product_id": "M500-0042",
    "confidence": 98.7,
    "inspected_at": "2026-04-07 17:58:12",
    "defect_type": "",
}


SORTER_STATE: dict[str, Any] = {
    "angle": 90.0,
    "direction": "good",
    "success": True,
    "count_good": 152,
    "count_bad": 8,
}


# web mockInspectionStandards 와 동일 (3종, KS 규격 맨홀뚜껑).
INSPECTION_STANDARDS: list[dict[str, Any]] = [
    {
        "product": "맨홀 뚜껑 KS D-600",
        "target": "외경 600mm / 두께 50mm",
        "tolerance": "±0.5mm",
        "threshold": "95.0%",
    },
    {
        "product": "맨홀 뚜껑 KS D-800",
        "target": "외경 800mm / 두께 60mm",
        "tolerance": "±0.8mm",
        "threshold": "95.0%",
    },
    {
        "product": "맨홀 뚜껑 KS D-450",
        "target": "외경 450mm / 두께 40mm",
        "tolerance": "±0.4mm",
        "threshold": "93.0%",
    },
]


# web mockProductionMetrics 의 (production, defectRate) 를 동일 30일 라벨로 표시.
PRODUCTION_VS_DEFECTS: list[dict[str, Any]] = [
    {"label": d, "production": p, "defect_rate": r}
    for (d, p, _b, r) in _PROD_METRICS_30D
]


WAREHOUSE_RACKS: list[dict[str, Any]] = [
    # 3행 × 6열, ID 1~18 (하단 좌측=1 → 우측 최상단=18).
    # 행 0 (하단): 1~6
    {"id": "1", "status": "full", "content": "맨홀뚜껑 M500", "qty": 24},
    {"id": "2", "status": "full", "content": "맨홀뚜껑 M600", "qty": 18},
    {"id": "3", "status": "partial", "content": "맨홀뚜껑 M600", "qty": 9},
    {"id": "4", "status": "full", "content": "그레이팅 GR-A", "qty": 32},
    {"id": "5", "status": "empty", "content": "", "qty": 0},
    {"id": "6", "status": "reserved", "content": "그레이팅 GR-B", "qty": 16},
    # 행 1 (중간): 7~12
    {"id": "7", "status": "partial", "content": "커버 CV-1", "qty": 4},
    {"id": "8", "status": "empty", "content": "", "qty": 0},
    {"id": "9", "status": "full", "content": "맨홀뚜껑 M400", "qty": 28},
    {"id": "10", "status": "locked", "content": "검사 대기", "qty": 6},
    {"id": "11", "status": "full", "content": "그레이팅 GR-C", "qty": 22},
    {"id": "12", "status": "partial", "content": "커버 CV-2", "qty": 7},
    # 행 2 (상단): 13~18
    {"id": "13", "status": "full", "content": "맨홀뚜껑 M800", "qty": 20},
    {"id": "14", "status": "reserved", "content": "출고 대기", "qty": 12},
    {"id": "15", "status": "full", "content": "그레이팅 GR-D", "qty": 26},
    {"id": "16", "status": "full", "content": "맨홀뚜껑 M500", "qty": 30},
    {"id": "17", "status": "partial", "content": "커버 CV-3", "qty": 5},
    {"id": "18", "status": "locked", "content": "품질 홀드", "qty": 8},
]


OUTBOUND_ORDERS: list[dict[str, Any]] = [
    {
        "id": "OUT-20260407-01",
        "product": "맨홀뚜껑 M500",
        "qty": 24,
        "customer": "대성산업",
        "policy": "FIFO",
        "status": "pending",
    },
    {
        "id": "OUT-20260407-02",
        "product": "그레이팅 GR-A",
        "qty": 30,
        "customer": "한진중공업",
        "policy": "FIFO",
        "status": "running",
    },
    {
        "id": "OUT-20260407-03",
        "product": "맨홀뚜껑 M800",
        "qty": 15,
        "customer": "포스코",
        "policy": "LIFO",
        "status": "pending",
    },
    {
        "id": "OUT-20260406-11",
        "product": "맨홀뚜껑 M600",
        "qty": 18,
        "customer": "현대건설",
        "policy": "FIFO",
        "status": "completed",
    },
    {
        "id": "OUT-20260406-09",
        "product": "커버 CV-2",
        "qty": 5,
        "customer": "삼성중공업",
        "policy": "FIFO",
        "status": "completed",
    },
]


PROCESS_PARAM_HISTORY: list[dict[str, Any]] = [
    {
        "time": "09:00:00",
        "stage": "용해",
        "temperature": 1452.3,
        "pressure": "-",
        "angle": "-",
        "power": "92%",
        "cooling": "-",
        "progress": "100%",
    },
    {
        "time": "09:00:00",
        "stage": "주형",
        "temperature": "-",
        "pressure": "85 bar",
        "angle": "-",
        "power": "-",
        "cooling": "-",
        "progress": "100%",
    },
    {
        "time": "09:15:00",
        "stage": "주탕",
        "temperature": 1400.0,
        "pressure": "-",
        "angle": "45°",
        "power": "-",
        "cooling": "-",
        "progress": "0%",
    },
    {
        "time": "09:30:00",
        "stage": "냉각",
        "temperature": 178.1,
        "pressure": "-",
        "angle": "-",
        "power": "-",
        "cooling": "60%",
        "progress": "81%",
    },
    {
        "time": "09:45:00",
        "stage": "탈형",
        "temperature": "-",
        "pressure": "-",
        "angle": "-",
        "power": "-",
        "cooling": "-",
        "progress": "0%",
    },
    {
        "time": "10:00:00",
        "stage": "후처리",
        "temperature": "-",
        "pressure": "-",
        "angle": "-",
        "power": "-",
        "cooling": "-",
        "progress": "67%",
    },
    {
        "time": "10:15:00",
        "stage": "검사",
        "temperature": "-",
        "pressure": "-",
        "angle": "-",
        "power": "-",
        "cooling": "-",
        "progress": "95%",
    },
    {
        "time": "10:30:00",
        "stage": "분류",
        "temperature": "-",
        "pressure": "-",
        "angle": "-",
        "power": "-",
        "cooling": "-",
        "progress": "0%",
    },
]


LIVE_PARAMETERS: dict[str, Any] = {
    "furnace_temperature": 1447.8,
    "furnace_target": 1450.0,
    "furnace_heating_power": 88.5,
    "mold_pressure": 82.3,
    "pour_angle": 42.5,
    "cooling_progress": 78.0,
    "cooling_current_temp": 178.1,
    "cooling_target_temp": 25.0,
    "cooling_remaining_min": 12,
    "mode_auto": True,
    "e_stop_active": False,
}


ORDER_ITEM_PROGRESS: list[dict[str, Any]] = [
    {"order_id": "ORD-2026-045", "product": "맨홀뚜껑 A형", "item": "A-1", "stage": "적재"},
    {"order_id": "ORD-2026-045", "product": "맨홀뚜껑 A형", "item": "A-2", "stage": "검사"},
    {"order_id": "ORD-2026-045", "product": "맨홀뚜껑 A형", "item": "A-3", "stage": "후처리"},
    {"order_id": "ORD-2026-045", "product": "맨홀뚜껑 A형", "item": "A-4", "stage": "후처리"},
    {"order_id": "ORD-2026-045", "product": "맨홀뚜껑 A형", "item": "A-5", "stage": "탈형"},
    {"order_id": "ORD-2026-045", "product": "맨홀뚜껑 A형", "item": "A-6", "stage": "주탕"},
    {"order_id": "ORD-2026-045", "product": "맨홀뚜껑 A형", "item": "A-7", "stage": "대기"},
    {"order_id": "ORD-2026-045", "product": "맨홀뚜껑 A형", "item": "A-8", "stage": "대기"},
    {"order_id": "ORD-2026-043", "product": "원형 그레이팅", "item": "B-1", "stage": "검사"},
    {"order_id": "ORD-2026-043", "product": "원형 그레이팅", "item": "B-2", "stage": "탈형"},
    {"order_id": "ORD-2026-043", "product": "원형 그레이팅", "item": "B-3", "stage": "주탕"},
    {"order_id": "ORD-2026-043", "product": "원형 그레이팅", "item": "B-4", "stage": "대기"},
]


RECENT_ORDERS: list[dict[str, Any]] = [
    {
        "id": "ORD-2026-045",
        "customer": "대성산업",
        "amount": 24_500_000,
        "due_date": "2026-04-15",
        "status": "production",
    },
    {
        "id": "ORD-2026-044",
        "customer": "한진중공업",
        "amount": 18_200_000,
        "due_date": "2026-04-12",
        "status": "approved",
    },
    {
        "id": "ORD-2026-043",
        "customer": "포스코",
        "amount": 42_800_000,
        "due_date": "2026-04-20",
        "status": "production",
    },
    {
        "id": "ORD-2026-042",
        "customer": "현대건설",
        "amount": 9_600_000,
        "due_date": "2026-04-10",
        "status": "completed",
    },
    {
        "id": "ORD-2026-041",
        "customer": "삼성중공업",
        "amount": 31_500_000,
        "due_date": "2026-04-18",
        "status": "reviewing",
    },
]
