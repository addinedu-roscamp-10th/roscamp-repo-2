"""AiResultImageSinkCommand unit tests — base64 디스크 저장 + URL 합성.

본 테스트는 DB / AI 서버 / HttpImageServer 없이 sink command 단독 동작만 검증.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from services.command.ai_result_image_sink_command import (
    AiResultImageSinkCommand,
    SavedAiResultImages,
)

# 1x1 단색 PNG (디코드 가능한 최소 PNG)
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe"
    b"\xa3\xc1\xeb\xe6\x00\x00\x00\x00IEND\xaeB`\x82"
)
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode("ascii")


def _make_sink(tmp_path: Path) -> AiResultImageSinkCommand:
    return AiResultImageSinkCommand(
        root=tmp_path,
        base_url="http://127.0.0.1:18800",
    )


def test_sink_writes_both_images_and_returns_urls(tmp_path: Path) -> None:
    """segmented + result 두 base64 모두 정상이면 파일 저장 + URL 두 개 반환."""
    sink = _make_sink(tmp_path)

    saved = sink.save(
        item_id=42,
        insp_txn_id=1001,
        segmented_image_b64=_PNG_B64,
        result_image_b64=_PNG_B64,
    )

    assert isinstance(saved, SavedAiResultImages)
    assert saved.segmented_path == tmp_path / "42" / "1001_segmented.png"
    assert saved.result_path == tmp_path / "42" / "1001_result.png"
    assert saved.segmented_path.exists()
    assert saved.result_path.exists()
    assert saved.segmented_path.read_bytes() == _PNG_BYTES
    assert saved.result_path.read_bytes() == _PNG_BYTES
    assert saved.segmented_url == "http://127.0.0.1:18800/inspections/42/1001_segmented.png"
    assert saved.result_url == "http://127.0.0.1:18800/inspections/42/1001_result.png"


def test_sink_returns_null_urls_when_b64_is_none(tmp_path: Path) -> None:
    """둘 다 None 이면 디스크 쓰기 없이 NULL URL 반환."""
    sink = _make_sink(tmp_path)

    saved = sink.save(
        item_id=42,
        insp_txn_id=1001,
        segmented_image_b64=None,
        result_image_b64=None,
    )

    assert saved.segmented_path is None
    assert saved.segmented_url is None
    assert saved.result_path is None
    assert saved.result_url is None
    # 디렉토리는 mkdir 되지만 파일은 없어야 함
    assert (tmp_path / "42").exists()
    assert not any((tmp_path / "42").iterdir())


def test_sink_handles_one_side_missing(tmp_path: Path) -> None:
    """segmented 만 있고 result 없으면 segmented 만 저장, result URL 은 None."""
    sink = _make_sink(tmp_path)

    saved = sink.save(
        item_id=7,
        insp_txn_id=99,
        segmented_image_b64=_PNG_B64,
        result_image_b64=None,
    )

    assert saved.segmented_path is not None
    assert saved.segmented_path.exists()
    assert saved.segmented_url == "http://127.0.0.1:18800/inspections/7/99_segmented.png"
    assert saved.result_path is None
    assert saved.result_url is None


def test_sink_strips_data_uri_prefix(tmp_path: Path) -> None:
    """`data:image/png;base64,...` 형태 입력도 정상 디코드."""
    sink = _make_sink(tmp_path)

    data_uri = f"data:image/png;base64,{_PNG_B64}"
    saved = sink.save(
        item_id=1,
        insp_txn_id=1,
        segmented_image_b64=data_uri,
        result_image_b64=None,
    )

    assert saved.segmented_path is not None
    assert saved.segmented_path.read_bytes() == _PNG_BYTES


def test_sink_rejects_invalid_base64(tmp_path: Path) -> None:
    """디코드 실패한 입력은 warning + URL None 으로 graceful 처리."""
    sink = _make_sink(tmp_path)

    saved = sink.save(
        item_id=1,
        insp_txn_id=1,
        segmented_image_b64="!!! not base64 !!!",
        result_image_b64=_PNG_B64,
    )

    # segmented 는 실패해도 result 는 정상 저장
    assert saved.segmented_path is None
    assert saved.segmented_url is None
    assert saved.result_path is not None
    assert saved.result_url is not None


def test_sink_rejects_invalid_ids(tmp_path: Path) -> None:
    """item_id 또는 insp_txn_id 가 비유효하면 빈 결과 반환."""
    sink = _make_sink(tmp_path)

    saved = sink.save(
        item_id=0,
        insp_txn_id=1,
        segmented_image_b64=_PNG_B64,
        result_image_b64=_PNG_B64,
    )

    assert saved.segmented_url is None
    assert saved.result_url is None
    assert not (tmp_path / "0").exists()


@pytest.mark.parametrize(
    ("env_host", "env_port", "expected"),
    [
        (None, None, "http://127.0.0.1:18800"),
        ("vision.local", None, "http://vision.local:18800"),
        (None, "9000", "http://127.0.0.1:9000"),
        ("vision.local", "9000", "http://vision.local:9000"),
    ],
)
def test_sink_default_base_url_respects_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env_host: str | None,
    env_port: str | None,
    expected: str,
) -> None:
    """MGMT_IMAGE_BASE_HOST / MGMT_IMAGE_HTTP_PORT env 가 base_url 합성에 반영."""
    monkeypatch.delenv("MGMT_IMAGE_BASE_HOST", raising=False)
    monkeypatch.delenv("MGMT_IMAGE_HTTP_PORT", raising=False)
    if env_host is not None:
        monkeypatch.setenv("MGMT_IMAGE_BASE_HOST", env_host)
    if env_port is not None:
        monkeypatch.setenv("MGMT_IMAGE_HTTP_PORT", env_port)

    sink = AiResultImageSinkCommand(root=tmp_path)
    assert sink.base_url == expected
