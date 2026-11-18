import os
from pathlib import Path
import tempfile

import pytest

ROOT = (
    Path(os.environ.get("FQDB_TEST_DATA_ROOT", "/home/quant/colorful-data/FreeQuantDB-test-artifacts/tests"))
    .expanduser()
    .resolve()
)


@pytest.fixture
def data_root():
    ROOT.mkdir(parents=True, exist_ok=True)
    # Keep evidence and all generated data outside dev, including pytest fixtures.
    return Path(tempfile.mkdtemp(dir=ROOT, prefix="case-"))


@pytest.fixture(autouse=True)
def isolated_user_selection(data_root, monkeypatch):
    # SDK initialization persists its choice for later processes. Keep that
    # state inside this test's SSD root, including inherited child processes.
    from fqdb import config

    monkeypatch.setenv("FQDB_USER_CONFIG", str(data_root / "user-config.json"))
    monkeypatch.setattr(config, "_current_root", None)
