from __future__ import annotations

import json
from pathlib import Path

import pytest

from support_agent.bootstrap import build_pipeline
from support_agent.config import PROJECT_ROOT, Settings
from support_agent.delivery.sender import FileSender
from support_agent.ingest.parse import parse_json
from support_agent.store.db import Store

SAMPLES = PROJECT_ROOT / "data" / "samples"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        llm_provider="fake",
        admin_token="test-token",
        log_json=False,
        log_level="WARNING",
        db_path=tmp_path / "t.sqlite3",
        outbox_dir=tmp_path / "out",
    )


@pytest.fixture
def sent_dir(tmp_path: Path) -> Path:
    return tmp_path / "sent"


@pytest.fixture
def pipeline(settings: Settings, sent_dir: Path):
    return build_pipeline(
        settings, store=Store(":memory:"), sender=FileSender(settings.support_address, sent_dir)
    )


def load_sample(name: str):
    path = next(SAMPLES.glob(f"{name}*.json"))
    return parse_json(json.loads(path.read_text(encoding="utf-8")))
