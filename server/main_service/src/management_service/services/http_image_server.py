"""검사 이미지 HTTP 서빙 — backend ↔ AI 서버 사이 URL 기반 이미지 전송용.

설계 배경:
    AI 서버는 backend Mac 과 다른 머신 (team2@100.66.177.119) 이므로 디스크 경로 공유가
    불가능하다. 새 contract (POST /inspect JSON {image_url, product_info}) 는 AI 서버가
    `image_url` 로 HTTP GET 하여 이미지를 가져오는 방식이라, backend 가 자신이 보관한
    검사 이미지를 HTTP 로 노출해야 한다.

엔드포인트:
    GET  /inspections/{item_id}/{filename}  → image/jpeg or image/png bytes
    GET  /health                            → 헬스체크

설계 원칙:
    - 별도 thread 에서 uvicorn 실행 (gRPC 서버 영향 없음)
    - 정적 파일 서빙만 — 인증/CORS/업로드 없음
    - traversal 차단: filename 에 '/' 또는 '..' 포함 시 404
    - 환경변수:
        MGMT_INSP_IMAGE_SAVE_DIR  - 이미지 저장 루트 (기본 /var/lib/casting/inspections)
        MGMT_IMAGE_HTTP_HOST      - bind host (기본 0.0.0.0)
        MGMT_IMAGE_HTTP_PORT      - bind port (기본 18800)
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

_DEFAULT_ROOT = "/var/lib/casting/inspections"
_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = 18800
_SUPPORTED_EXTS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def _build_app(root: Path) -> FastAPI:
    """현 root 를 capture 한 FastAPI app 인스턴스 생성."""
    app = FastAPI(title="Casting Inspection Image Server")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "root": str(root)}

    @app.get("/inspections/{item_id}/{filename}")
    async def get_image(item_id: str, filename: str) -> FileResponse:
        # path traversal 차단
        if "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(status_code=400, detail="invalid_filename")
        if "/" in item_id or "\\" in item_id or ".." in item_id:
            raise HTTPException(status_code=400, detail="invalid_item_id")

        suffix = Path(filename).suffix.lower()
        if suffix not in _SUPPORTED_EXTS:
            raise HTTPException(status_code=400, detail=f"unsupported_extension:{suffix}")

        target = (root / item_id / filename).resolve()
        # root 밖으로 escape 차단 (심볼릭 링크 등)
        try:
            target.relative_to(root.resolve())
        except ValueError:
            raise HTTPException(status_code=400, detail="path_outside_root") from None

        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail=f"image_not_found:{item_id}/{filename}")

        return FileResponse(target, media_type=_SUPPORTED_EXTS[suffix])

    return app


class HttpImageServer:
    """이미지 정적 서빙용 uvicorn 서버 — 별도 thread 에서 실행."""

    def __init__(
        self,
        root: str | Path | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        env_root = root or os.environ.get("MGMT_INSP_IMAGE_SAVE_DIR", _DEFAULT_ROOT)
        self._root = Path(env_root).resolve()
        self._host = host or os.environ.get("MGMT_IMAGE_HTTP_HOST", _DEFAULT_HOST)
        try:
            self._port = int(port if port is not None else os.environ.get("MGMT_IMAGE_HTTP_PORT", _DEFAULT_PORT))
        except (TypeError, ValueError):
            self._port = _DEFAULT_PORT
        self._server: Optional[object] = None  # uvicorn.Server (lazy import)
        self._thread: Optional[threading.Thread] = None
        self._started = False

    @property
    def root(self) -> Path:
        return self._root

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url(self) -> str:
        """다른 모듈이 image_url 합성 시 사용 — 외부 노출 host 는 env 로 별도 설정."""
        external_host = os.environ.get("MGMT_IMAGE_BASE_HOST", "127.0.0.1")
        return f"http://{external_host}:{self._port}"

    def start(self) -> None:
        """서버 thread 시작. 이미 실행 중이면 no-op."""
        if self._started:
            return
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "HttpImageServer: mkdir 실패 root=%s exc=%s — 서빙 진행 (디스크 권한 확인 필요)",
                self._root, exc,
            )
        try:
            import uvicorn  # type: ignore
        except ImportError:
            logger.warning("HttpImageServer: uvicorn 미설치 — 서버 시작 skip")
            return

        app = _build_app(self._root)
        config = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)

        def _run() -> None:
            try:
                self._server.run()
            except Exception as exc:  # noqa: BLE001
                logger.warning("HttpImageServer: 종료 with exc=%s", exc)

        self._thread = threading.Thread(target=_run, name="http-image-server", daemon=True)
        self._thread.start()
        self._started = True
        logger.info(
            "HttpImageServer 시작: bind=%s:%d root=%s base_url=%s",
            self._host, self._port, self._root, self.base_url,
        )

    def stop(self) -> None:
        if not self._started or self._server is None:
            return
        try:
            self._server.should_exit = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("HttpImageServer.stop: should_exit 설정 실패 exc=%s", exc)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._started = False
        logger.info("HttpImageServer 종료")
