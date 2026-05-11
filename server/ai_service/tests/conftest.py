"""Shared pytest fixtures for AI service tests — 임시 저장소 + round_robin 엔진."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def tmp_inspection_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """AI_INSP_IMAGE_ROOT 를 임시 디렉터리로 격리."""
    root = tmp_path / "Inspection_Image"
    monkeypatch.setenv("AI_INSP_IMAGE_ROOT", str(root))
    return root


@pytest.fixture()
def fixed_round_robin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_MOCK_MODE", "round_robin")
    monkeypatch.delenv("AI_MOCK_PASS_RATIO", raising=False)
