import os
from pathlib import Path
import tempfile

import pytest

# Keep test collection and existing explicit lifecycle fixtures passive. The
# default startup tests opt back in inside subprocesses with isolated homes.
os.environ["FQDB_AUTOSTART"] = "0"

from gateway_helpers import loopback_config

ROOT = (
    Path(os.environ.get("FQDB_TEST_DATA_ROOT", "/home/quant/colorful-data/FreeQuantDB-test-artifacts/tests"))
    .expanduser()
    .resolve()
)


@pytest.fixture
def empty_data_root():
    ROOT.mkdir(parents=True, exist_ok=True)
    # Keep evidence and all generated data outside dev, including pytest fixtures.
    return Path(tempfile.mkdtemp(dir=ROOT, prefix="case-"))


@pytest.fixture(autouse=True)
def isolated_user_selection(empty_data_root, monkeypatch):
    # SDK initialization persists its choice for later processes. Keep that
    # state and the default home fallback inside this test's SSD root.
    from fqdb import config

    monkeypatch.delenv("FQDB_DATA_DIR", raising=False)
    home = str(empty_data_root / "SYNTHETIC-home")
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    monkeypatch.setenv("FQDB_USER_CONFIG", str(empty_data_root / "user-config.json"))
    monkeypatch.setattr(config, "_current_root", None)


@pytest.fixture
def data_root(empty_data_root):
    return loopback_config(empty_data_root).root
