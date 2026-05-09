"""Jetson on-demand inspection capture (SPEC-MGMTV2-INSPECTION-PUSH).

ToF2 감지 시 1 frame 만 잡아 JPEG bytes 로 반환. 서버는 이 bytes 를
ManagementServiceV2.UploadInspectionImage RPC 로 push 받아 robot_adapter
가 로컬 캐시 + AI Server SCP forward 한다.

설계 결정 (2026-05-06, 사용자 확정):
    - 라이브러리: OpenCV (cv2.VideoCapture) + cv2.imencode('.jpg')
    - 디바이스: 기본 ``/dev/video0`` (env MGMT_CAM_INSP_DEVICE 로 override)
    - 해상도/품질: env (MGMT_CAM_INSP_WIDTH/HEIGHT/JPEG_QUALITY)
    - cv2 미설치 환경 (CI/Mac dev): placeholder bytes 반환 → 통합 e2e 가
      RPC 노출만 검증할 수 있게 한다.

환경변수:
    MGMT_CAM_INSP_DEVICE       기본 "/dev/video0"
    MGMT_CAM_INSP_WIDTH        기본 1920
    MGMT_CAM_INSP_HEIGHT       기본 1080
    MGMT_CAM_INSP_JPEG_QUALITY 기본 90 (1-100)
    MGMT_CAM_INSP_WARMUP       기본 0  (warmup 프레임 수 — auto exposure 안정화용, 0=비활성)
    MGMT_CAM_INSP_DEBUG_PLACEHOLDER  "1" 시 cv2 가 있어도 placeholder 사용 (테스트용)
    MGMT_CAM_INSP_SAVE_DIR     기본 비어있음(저장 안 함) — 디렉터리 지정 시 캡처된 JPEG 을
                                해당 디렉터리에 저장. 파일명 규칙은 호출 측에서 넘긴
                                rfid_label 우선, 없으면 YYYY_MMDD_HHMM_<0.1ms*4>.jpg.
    MGMT_CAM_INSP_MAX_FILES    기본 10 — save dir 에 유지할 최대 .jpg 파일 수.
                                저장 직후 mtime 기준 가장 오래된 파일부터 삭제(rotation).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ---- Background grabber (모듈 전역) -------------------------------
# publisher 가 시작할 때 start_background_grabber() 호출 → 카메라를 항상 open 한
# 채 별도 thread 가 latest frame 을 갱신. capture_jpeg() 는 그 frame 을 즉시 사용
# (cold-start latency 회피, AE/AWB 항상 안정 상태).
_grabber: "BackgroundGrabber | None" = None
_grabber_lock = threading.Lock()

# GStreamer 기반 grabber — raw MJPG bytes 보존 (cv2 디코드/재인코드 우회)
_gst_grabber: "GstBackgroundGrabber | None" = None
_gst_grabber_lock = threading.Lock()

# ---- env 상수 (모듈 import 시 1회 평가) -------------------------------
DEFAULT_DEVICE = "/dev/video0"
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_JPEG_QUALITY = 90
DEFAULT_WARMUP_FRAMES = 0
DEFAULT_MAX_FILES = 10

# 파일명 안전화: 영숫자/언더스코어/하이픈/점 외 문자는 모두 '_' 로 치환
_FILENAME_SAFE_RE = re.compile(r"[^\w.\-]")


@dataclass(frozen=True)
class CaptureResult:
    """capture_jpeg() 반환값."""

    ok: bool
    jpeg_bytes: bytes
    width: int
    height: int
    captured_at_unix: float
    reason: str


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("env %s=%r 정수 변환 실패 — 기본값 %d 사용", name, raw, default)
        return default


class BackgroundGrabber:
    """USB 카메라를 항상 open 상태로 유지하면서 별도 thread 가 latest frame 을 갱신.

    capture_jpeg() 가 호출되면 latest frame 을 즉시 가져와 encode (cold-start 회피).
    publisher.main() 에서 start_background_grabber() 로 한 번 시작.

    산업용 검사 베스트 프랙티스: AE/AWB 매뉴얼 고정 → 일관된 노출/색감.
    env 변수가 설정되어 있으면 v4l2-ctl 로 매뉴얼 모드 적용:
        MGMT_CAM_INSP_EXPOSURE_ABSOLUTE  exposure_absolute (1~10000), 비어있으면 auto 유지
        MGMT_CAM_INSP_WB_TEMP            white_balance_temperature (2800~6500), 비어있으면 auto
        MGMT_CAM_INSP_POWER_LINE_FREQ    power_line_frequency (0=disable, 1=50Hz, 2=60Hz)
    """

    def __init__(self, device: str, width: int, height: int) -> None:
        import cv2  # type: ignore[import-not-found]

        self._cv2 = cv2
        cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(device)
        if not cap.isOpened():
            raise RuntimeError(f"BackgroundGrabber: cannot open {device}")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.cap = cap
        self.device = device
        # V4L2 controls 적용 (open 직후, thread 시작 전)
        self._apply_v4l2_controls(device)
        self._lock = threading.Lock()
        self._latest_frame = None
        self._latest_at = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="cam-bg-grabber", daemon=True,
        )

    def _apply_v4l2_controls(self, device: str) -> None:
        """v4l2-ctl 로 manual exposure / WB / power-line freq 적용.

        env 변수가 비어있으면 해당 항목은 auto 유지. 적용 순서가 중요:
            1. exposure_auto=1 (manual mode 진입) → 그 후 exposure_absolute 적용
            2. white_balance_temperature_auto=0 → 그 후 white_balance_temperature 적용
        v4l2-ctl 미설치 / 미지원 카메라는 warning 만 남기고 계속 진행.
        """
        import shutil
        import subprocess

        if shutil.which("v4l2-ctl") is None:
            logger.info(
                "[v4l2-ctl] 미설치 — manual control 적용 skip "
                "(apt install v4l-utils 필요)"
            )
            return

        commands: list[list[str]] = []

        plf = os.getenv("MGMT_CAM_INSP_POWER_LINE_FREQ", "").strip()
        if plf:
            commands.append([f"power_line_frequency={plf}"])

        exposure_abs = os.getenv("MGMT_CAM_INSP_EXPOSURE_ABSOLUTE", "").strip()
        if exposure_abs:
            commands.append(["exposure_auto=1"])
            commands.append([f"exposure_absolute={exposure_abs}"])
            commands.append(["exposure_auto_priority=0"])

        wb_temp = os.getenv("MGMT_CAM_INSP_WB_TEMP", "").strip()
        if wb_temp:
            commands.append(["white_balance_temperature_auto=0"])
            commands.append([f"white_balance_temperature={wb_temp}"])

        # 추가 ISP 컨트롤 — 디테일/명암/감마/채도 미세 조정
        for env_key, ctrl_name in [
            ("MGMT_CAM_INSP_SHARPNESS", "sharpness"),
            ("MGMT_CAM_INSP_CONTRAST", "contrast"),
            ("MGMT_CAM_INSP_GAMMA", "gamma"),
            ("MGMT_CAM_INSP_SATURATION", "saturation"),
            ("MGMT_CAM_INSP_BRIGHTNESS", "brightness"),
        ]:
            val = os.getenv(env_key, "").strip()
            if val:
                commands.append([f"{ctrl_name}={val}"])

        if not commands:
            logger.info(
                "[v4l2-ctl] manual control env 미설정 — auto AE/AWB 유지"
            )
            return

        for ctrl in commands:
            try:
                r = subprocess.run(
                    ["v4l2-ctl", "-d", device, "-c", ctrl[0]],
                    capture_output=True, text=True, timeout=2.0,
                )
                if r.returncode != 0:
                    logger.warning(
                        "[v4l2-ctl] -c %s failed (rc=%d): %s",
                        ctrl[0], r.returncode, r.stderr.strip(),
                    )
                else:
                    logger.info("[v4l2-ctl] -c %s applied", ctrl[0])
            except Exception as e:  # noqa: BLE001
                logger.warning("[v4l2-ctl] -c %s exception: %s", ctrl[0], e)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        try:
            self.cap.release()
        except Exception:  # noqa: BLE001
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            ok, frame = self.cap.read()
            if ok and frame is not None:
                with self._lock:
                    self._latest_frame = frame
                    self._latest_at = time.time()
            else:
                time.sleep(0.05)

    def latest(self):
        with self._lock:
            if self._latest_frame is None:
                return None, 0.0
            return self._latest_frame.copy(), self._latest_at


class GstBackgroundGrabber:
    """GStreamer 기반 라이브 스트림 grabber — raw MJPG bytes 직접 사용.

    카메라가 출력하는 MJPG bytes 를 cv2 디코드 / 재인코드 없이 그대로 받아 디스크
    저장 → quality loss 0. opencv path 의 BGR conversion + JPEG re-encode 단계를
    완전히 우회.

    PyGObject (gi.repository.Gst) 의존. Jetson 시스템 패키지로 설치되어 있음.
    """

    def __init__(self, device: str, width: int, height: int) -> None:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst, GLib
        Gst.init(None)
        self._Gst = Gst
        self._GLib = GLib

        pipeline_str = (
            f"v4l2src device={device} ! "
            f"image/jpeg,width={width},height={height},framerate=30/1 ! "
            f"appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
        )
        self.pipeline = Gst.parse_launch(pipeline_str)
        self.sink = self.pipeline.get_by_name("sink")
        self.sink.connect("new-sample", self._on_new_sample)
        self.device = device
        self.width = width
        self.height = height

        # V4L2 controls 적용 (pipeline 시작 전 — device 비점유 상태)
        # cv2 BackgroundGrabber 의 _apply_v4l2_controls 와 동일 로직.
        BackgroundGrabber._apply_v4l2_controls(self, device)  # type: ignore[arg-type]

        self._lock = threading.Lock()
        self._latest_bytes: bytes | None = None
        self._latest_at = 0.0
        self._loop = GLib.MainLoop()
        self._loop_thread = threading.Thread(
            target=self._loop.run, name="gst-mainloop", daemon=True,
        )

    def _on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return self._Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(self._Gst.MapFlags.READ)
        if ok:
            data = bytes(mapinfo.data)
            buf.unmap(mapinfo)
            with self._lock:
                self._latest_bytes = data
                self._latest_at = time.time()
        return self._Gst.FlowReturn.OK

    def start(self) -> None:
        self.pipeline.set_state(self._Gst.State.PLAYING)
        self._loop_thread.start()

    def stop(self) -> None:
        try:
            self.pipeline.set_state(self._Gst.State.NULL)
        except Exception:  # noqa: BLE001
            pass
        if self._loop.is_running():
            self._loop.quit()
        self._loop_thread.join(timeout=2.0)

    def latest(self):
        with self._lock:
            if self._latest_bytes is None:
                return None, 0.0
            return self._latest_bytes, self._latest_at


def start_gst_background_grabber(
    *,
    device: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> "GstBackgroundGrabber | None":
    """GStreamer raw MJPG grabber 시작. cv2 BackgroundGrabber 와 양자택일."""
    global _gst_grabber
    with _gst_grabber_lock:
        if _gst_grabber is not None:
            return _gst_grabber
        device = (
            device or os.getenv("MGMT_CAM_INSP_DEVICE", "").strip() or DEFAULT_DEVICE
        )
        width = width if width is not None else _env_int("MGMT_CAM_INSP_WIDTH", DEFAULT_WIDTH)
        height = height if height is not None else _env_int("MGMT_CAM_INSP_HEIGHT", DEFAULT_HEIGHT)
        try:
            grabber = GstBackgroundGrabber(device=device, width=width, height=height)
        except Exception as e:  # noqa: BLE001
            logger.error("[gst_grabber] 시작 실패: %s", e)
            return None
        grabber.start()
        _gst_grabber = grabber
        logger.info(
            "[gst_grabber] started: device=%s %dx%d (raw MJPG)",
            device, width, height,
        )
        return _gst_grabber


def stop_gst_background_grabber() -> None:
    global _gst_grabber
    with _gst_grabber_lock:
        if _gst_grabber is not None:
            _gst_grabber.stop()
            _gst_grabber = None
            logger.info("[gst_grabber] stopped")


def start_background_grabber(
    *,
    device: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> "BackgroundGrabber | None":
    """publisher 가 한 번만 호출. 이후 capture_jpeg 는 자동으로 latest frame 사용."""
    global _grabber
    with _grabber_lock:
        if _grabber is not None:
            return _grabber
        device = (
            device or os.getenv("MGMT_CAM_INSP_DEVICE", "").strip() or DEFAULT_DEVICE
        )
        width = width if width is not None else _env_int("MGMT_CAM_INSP_WIDTH", DEFAULT_WIDTH)
        height = height if height is not None else _env_int("MGMT_CAM_INSP_HEIGHT", DEFAULT_HEIGHT)
        try:
            grabber = BackgroundGrabber(device=device, width=width, height=height)
        except Exception as e:  # noqa: BLE001
            logger.error("[capture_inspection] BackgroundGrabber 시작 실패: %s", e)
            return None
        grabber.start()
        _grabber = grabber
        logger.info(
            "[capture_inspection] background grabber started: device=%s %dx%d",
            device, width, height,
        )
        return _grabber


def stop_background_grabber() -> None:
    global _grabber
    with _grabber_lock:
        if _grabber is not None:
            _grabber.stop()
            _grabber = None
            logger.info("[capture_inspection] background grabber stopped")


def _build_jpeg_encode_param(jpeg_quality: int):
    """JPEG encode 옵션 구성. quality + optimize + (옵션) chroma 4:4:4.

    MGMT_CAM_INSP_JPEG_CHROMA_444=1 시 chroma subsampling 비활성 (4:4:4).
    OpenCV 4.6+ 에서 IMWRITE_JPEG_SAMPLING_FACTOR 지원. 미지원 빌드는 gracefully skip.
    """
    import cv2
    params = [
        int(cv2.IMWRITE_JPEG_QUALITY), max(1, min(100, jpeg_quality)),
        int(cv2.IMWRITE_JPEG_OPTIMIZE), 1,
    ]
    if os.getenv("MGMT_CAM_INSP_JPEG_CHROMA_444", "0").strip() in ("1", "true", "yes"):
        if hasattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR"):
            # 4:4:4 = no chroma subsampling. encoded as 0x111111
            params += [int(cv2.IMWRITE_JPEG_SAMPLING_FACTOR), 0x111111]
    return params


def _enhance_image(img):
    """CLAHE + Unsharp mask 로 디테일 강화. 원본 손상 없이 새 ndarray 반환.

    LAB color space 의 L 채널에만 CLAHE 적용 → 색감(a/b 채널) 보존.
    그 후 GaussianBlur 기반 unsharp mask 로 선명도 강화.
    AI 검사용 후처리. raw 이미지가 따로 필요하면 이 함수 적용 전 frame 을 별도 저장.
    """
    import cv2
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l_ch = clahe.apply(l_ch)
    lab = cv2.merge([l_ch, a_ch, b_ch])
    enhanced = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    blur = cv2.GaussianBlur(enhanced, (0, 0), sigmaX=2.0)
    return cv2.addWeighted(enhanced, 1.5, blur, -0.5, 0)


def _placeholder_jpeg() -> bytes:
    """cv2 미설치 또는 디버그 모드에서 사용할 minimal JPEG (1x1 px black).

    e2e 테스트가 RPC 통신 자체만 검증할 수 있도록 zero-dependency.
    """
    # 1x1 px black JPEG (RFC 표준 JFIF 헤더 + 최소 SOI/EOI 마커)
    return bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605"
        "08070707090908"
        "0a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c"
        "2837292c30313434"
        "1f27393d38323c2e333432ffc0000b080001000101011100ffc4001f0000010"
        "501010101010100000000000000"
        "0001020304050607080a0bffc400b510000201030302040305050404000001"
        "7d01020300041105122131410613"
        "516107227114328191a1082342b1c11552d1f02433627282090a161718191a"
        "25262728292a3435363738393a43"
        "4445464748494a535455565758595a636465666768696a737475767778797a"
        "838485868788898a92939495969798"
        "999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3"
        "d4d5d6d7d8d9dae1e2e3e4e5e6e7"
        "e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbd0ffd9"
    )


def _safe_label(label: str) -> str:
    """파일명에 안전한 문자만 남김. 영숫자/'_'/'-'/'.' 외에는 '_' 치환.

    NDEF Text 가 'order_1_item_20260417_1' 처럼 안전한 문자만 사용하지만,
    예기치 못한 입력 (콜론, 슬래시, 공백 등) 도 방어적으로 sanitize.
    """
    cleaned = _FILENAME_SAFE_RE.sub("_", label).strip("._-")
    return cleaned


def _compose_capture_filename(
    rfid_label: str | None, captured_at: float,
) -> str:
    """캡처 파일명 결정.

    1) rfid_label (NDEF text) 가 비어있지 않고 sanitize 후 토큰이 남으면 → '<label>.jpg'
    2) 없으면 → 'YYYY_MMDD_HHMM_<0.1ms*4>.jpg' (사용자 정의 timestamp 형식)

    timestamp 포맷의 마지막 4자리는 초의 분수 부분을 0.1ms 단위로 표현 (0000~9999).
    예: captured_at=1746692644.18203 → '..._1820'.
    """
    if rfid_label:
        sanitized = _safe_label(rfid_label.strip())
        if sanitized:
            return f"{sanitized}.jpg"
    dt = datetime.fromtimestamp(captured_at)
    sub = int((captured_at - int(captured_at)) * 10000)
    sub = max(0, min(9999, sub))
    return f"{dt:%Y_%m%d_%H%M}_{sub:04d}.jpg"


def _rotate_capture_dir(save_root: Path, max_files: int) -> None:
    """save_root 의 .jpg 파일 중 mtime 기준 오래된 것부터 삭제, 최신 max_files 만 유지.

    max_files <= 0 이면 회전 비활성. 디렉터리 access 실패는 silent skip
    (capture 결과에 영향 없도록 best-effort).
    """
    if max_files <= 0:
        return
    try:
        files = list(save_root.glob("*.jpg"))
    except OSError:
        return
    if len(files) <= max_files:
        return
    try:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in files[max_files:]:
        try:
            old.unlink()
            logger.info(
                "[capture_inspection] rotated old capture: %s", old.name,
            )
        except OSError as e:
            logger.warning(
                "[capture_inspection] rotation 실패 %s: %s", old.name, e,
            )


def _persist_capture(
    save_dir: str,
    *,
    captured_at: float,
    rfid_label: str | None,
    primary_bytes: bytes,
    enhanced_bytes: bytes | None = None,
    log_tag: str = "",
) -> Path | None:
    """JPEG bytes 를 save_dir 에 저장하고 rotation 수행.

    - 파일명은 _compose_capture_filename 결정.
    - enhanced_bytes 가 있으면 동일 stem 에 '_enh' 접미 추가 후 저장.
    - 저장 후 MGMT_CAM_INSP_MAX_FILES (기본 10) 까지만 유지하도록 rotation.
    - I/O 오류는 warning 만 남기고 None 반환 (캡처 결과 영향 없음).
    """
    try:
        save_root = Path(save_dir)
        save_root.mkdir(parents=True, exist_ok=True)
        filename = _compose_capture_filename(rfid_label, captured_at)
        primary_path = save_root / filename
        primary_path.write_bytes(primary_bytes)
        prefix = f" {log_tag}" if log_tag else ""
        logger.info(
            "[capture_inspection]%s saved to %s (%d bytes)",
            prefix, primary_path, len(primary_bytes),
        )
        if enhanced_bytes:
            enh_path = primary_path.with_name(
                f"{primary_path.stem}_enh{primary_path.suffix}",
            )
            enh_path.write_bytes(enhanced_bytes)
            logger.info(
                "[capture_inspection]%s (enh) saved to %s (%d bytes)",
                prefix, enh_path, len(enhanced_bytes),
            )
        max_files = _env_int("MGMT_CAM_INSP_MAX_FILES", DEFAULT_MAX_FILES)
        _rotate_capture_dir(save_root, max_files)
        return primary_path
    except OSError as e:
        prefix = f" {log_tag}" if log_tag else ""
        logger.warning(
            "[capture_inspection]%s disk save failed (dir=%s): %s",
            prefix, save_dir, e,
        )
        return None


def capture_jpeg(
    *,
    device: str | None = None,
    width: int | None = None,
    height: int | None = None,
    jpeg_quality: int | None = None,
    warmup_frames: int | None = None,
    rfid_label: str | None = None,
) -> CaptureResult:
    """USB 카메라에서 1 frame 캡처 후 JPEG bytes 로 인코딩.

    cv2 가 import 가능하면 실 capture, 아니면 placeholder.

    Args:
        device: V4L2 device path. None 이면 env MGMT_CAM_INSP_DEVICE 또는 "/dev/video0".
        width/height: 요청 해상도. None 이면 env 또는 1920x1080.
        jpeg_quality: 1-100. None 이면 env 또는 90.
        warmup_frames: capture 전 grab + discard 회수 (auto exposure 안정화). 0 이면 비활성.
        rfid_label: 디스크 저장 시 파일명으로 사용할 RFID NDEF text. None 이면 timestamp 기반 파일명.

    Returns:
        CaptureResult.

    Notes:
        - cv2.VideoCapture(/dev/video0) 가 실패하면 ok=False + placeholder 반환.
        - jpeg_bytes 는 항상 non-empty (placeholder 라도) — 호출 측이 빈 bytes 분기를 따로 둘 필요 없음.
        - 다중 thread 동시 호출 안전성 보장 안 함 — esp_bridge drain thread 단독 호출 가정.
    """
    device = (device or os.getenv("MGMT_CAM_INSP_DEVICE", "").strip() or DEFAULT_DEVICE)
    width = width if width is not None else _env_int("MGMT_CAM_INSP_WIDTH", DEFAULT_WIDTH)
    height = height if height is not None else _env_int("MGMT_CAM_INSP_HEIGHT", DEFAULT_HEIGHT)
    jpeg_quality = (
        jpeg_quality if jpeg_quality is not None
        else _env_int("MGMT_CAM_INSP_JPEG_QUALITY", DEFAULT_JPEG_QUALITY)
    )
    warmup_frames = (
        warmup_frames if warmup_frames is not None
        else _env_int("MGMT_CAM_INSP_WARMUP", DEFAULT_WARMUP_FRAMES)
    )

    captured_at = time.time()
    debug_placeholder = os.getenv("MGMT_CAM_INSP_DEBUG_PLACEHOLDER", "0").strip() == "1"

    if debug_placeholder:
        logger.info("[capture_inspection] DEBUG_PLACEHOLDER=1 → placeholder JPEG 반환")
        return CaptureResult(
            ok=True,
            jpeg_bytes=_placeholder_jpeg(),
            width=1,
            height=1,
            captured_at_unix=captured_at,
            reason="debug_placeholder",
        )

    # ---- Fast path 0: GstBackgroundGrabber 활성 시 raw MJPG bytes 그대로 사용 ----
    gst_grabber = _gst_grabber
    if gst_grabber is not None:
        raw_bytes, latest_at = gst_grabber.latest()
        if raw_bytes is not None:
            captured_at = latest_at if latest_at > 0 else captured_at
            actual_w = gst_grabber.width
            actual_h = gst_grabber.height
            save_dir = os.getenv("MGMT_CAM_INSP_SAVE_DIR", "").strip()
            if save_dir:
                _persist_capture(
                    save_dir,
                    captured_at=captured_at,
                    rfid_label=rfid_label,
                    primary_bytes=raw_bytes,
                    log_tag="(gst)",
                )
            return CaptureResult(
                ok=True, jpeg_bytes=raw_bytes,
                width=actual_w, height=actual_h,
                captured_at_unix=captured_at, reason="gst_raw_mjpg",
            )
        else:
            logger.warning(
                "[capture_inspection] gst grabber 활성이지만 frame 미준비 — fallthrough"
            )

    # ---- Fast path 1: cv2 BackgroundGrabber 활성이면 latest frame 즉시 사용 ----
    grabber = _grabber
    if grabber is not None:
        try:
            import cv2  # type: ignore[import-not-found]
        except ImportError:
            cv2 = None  # type: ignore[assignment]
        if cv2 is None:
            logger.warning(
                "[capture_inspection] cv2 미설치 — placeholder JPEG 반환"
            )
            return CaptureResult(
                ok=False, jpeg_bytes=_placeholder_jpeg(),
                width=1, height=1, captured_at_unix=captured_at,
                reason="cv2 not installed",
            )
        frame, latest_at = grabber.latest()
        if frame is None:
            logger.warning(
                "[capture_inspection] background grabber 활성이지만 frame 미준비"
            )
            # fallthrough → 단발 capture path
        else:
            actual_h, actual_w = int(frame.shape[0]), int(frame.shape[1])
            encode_param = _build_jpeg_encode_param(jpeg_quality)
            ok_enc, buf = cv2.imencode(".jpg", frame, encode_param)
            if not ok_enc:
                return CaptureResult(
                    ok=False, jpeg_bytes=_placeholder_jpeg(),
                    width=actual_w, height=actual_h,
                    captured_at_unix=captured_at, reason="cv2.imencode failed (bg)",
                )
            jpeg_bytes = bytes(buf.tobytes())
            captured_at = latest_at if latest_at > 0 else captured_at

            # 디스크 저장 (best-effort)
            save_dir = os.getenv("MGMT_CAM_INSP_SAVE_DIR", "").strip()
            if save_dir:
                enhanced_bytes: bytes | None = None
                if os.getenv("MGMT_CAM_INSP_ENHANCE", "0").strip() in ("1", "true", "yes"):
                    try:
                        enh_frame = _enhance_image(frame)
                        ok_e, buf_e = cv2.imencode(
                            ".jpg", enh_frame, encode_param,
                        )
                        if ok_e:
                            enhanced_bytes = bytes(buf_e.tobytes())
                    except Exception as ee:  # noqa: BLE001
                        logger.warning(
                            "[capture_inspection] (bg+enh) encode failed: %s", ee,
                        )
                _persist_capture(
                    save_dir,
                    captured_at=captured_at,
                    rfid_label=rfid_label,
                    primary_bytes=jpeg_bytes,
                    enhanced_bytes=enhanced_bytes,
                    log_tag="(bg)",
                )

            return CaptureResult(
                ok=True, jpeg_bytes=jpeg_bytes,
                width=actual_w, height=actual_h,
                captured_at_unix=captured_at, reason="background_grabber",
            )

    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "[capture_inspection] cv2 미설치 — placeholder JPEG 반환 (Jetson 본체에서 "
            "pip install opencv-python 필요)"
        )
        return CaptureResult(
            ok=False,
            jpeg_bytes=_placeholder_jpeg(),
            width=1,
            height=1,
            captured_at_unix=captured_at,
            reason="cv2 not installed",
        )

    # V4L2 backend 명시 — publisher.py 와 동일 (latest frame 보장에 유리)
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap.release()
        cap = cv2.VideoCapture(device)
    if not cap.isOpened():
        cap.release()
        logger.error("[capture_inspection] VideoCapture open 실패: device=%s", device)
        return CaptureResult(
            ok=False,
            jpeg_bytes=_placeholder_jpeg(),
            width=1,
            height=1,
            captured_at_unix=captured_at,
            reason=f"device open failed: {device}",
        )

    try:
        # 해상도 요청 (디바이스가 거부하면 실제 frame.shape 가 다를 수 있음 — 후처리에서 검증)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
        # 백엔드 버퍼 1 frame 으로 제한 — 오래된 frame 이 latest 로 잡히는 것 방지
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        # 카메라 open 직후 AE/AWB 가 미보정 상태이므로 grab+discard 로 적응 시간 부여.
        # 일반 USB 웹캠은 ~10-30 프레임 (= 0.5~1.5초 @30fps) 정도 필요.
        for _ in range(max(0, warmup_frames)):
            cap.grab()

        ok, frame = cap.read()
        if not ok or frame is None:
            return CaptureResult(
                ok=False,
                jpeg_bytes=_placeholder_jpeg(),
                width=1,
                height=1,
                captured_at_unix=captured_at,
                reason="cap.read() returned no frame",
            )

        actual_h, actual_w = int(frame.shape[0]), int(frame.shape[1])

        # JPEG encode
        encode_param = _build_jpeg_encode_param(jpeg_quality)
        ok_enc, buf = cv2.imencode(".jpg", frame, encode_param)
        if not ok_enc:
            return CaptureResult(
                ok=False,
                jpeg_bytes=_placeholder_jpeg(),
                width=actual_w,
                height=actual_h,
                captured_at_unix=captured_at,
                reason="cv2.imencode failed",
            )

        jpeg_bytes = bytes(buf.tobytes())

        # 디스크 저장 (best-effort, 실패해도 capture 결과 영향 없음)
        # MGMT_CAM_INSP_SAVE_DIR 가 설정된 경우에만 활성화
        save_dir = os.getenv("MGMT_CAM_INSP_SAVE_DIR", "").strip()
        if save_dir:
            _persist_capture(
                save_dir,
                captured_at=captured_at,
                rfid_label=rfid_label,
                primary_bytes=jpeg_bytes,
                log_tag="",
            )

        return CaptureResult(
            ok=True,
            jpeg_bytes=jpeg_bytes,
            width=actual_w,
            height=actual_h,
            captured_at_unix=captured_at,
            reason="",
        )
    finally:
        cap.release()
