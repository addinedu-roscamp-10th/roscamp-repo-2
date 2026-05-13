"""uvicorn 진입점: `python -m ai_service` 로 실행."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("AI_SERVICE_HOST", "127.0.0.1")
    port = int(os.environ.get("AI_SERVICE_PORT", "8100"))
    log_level = os.environ.get("AI_SERVICE_LOG_LEVEL", "info")
    uvicorn.run(
        "ai_service.main:app",
        host=host,
        port=port,
        log_level=log_level,
        access_log=False,
    )


if __name__ == "__main__":
    main()
