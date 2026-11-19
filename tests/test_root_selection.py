"""Synthetic storage checks, including a mount path resolving to another disk."""

import json
import os
import sys

import pytest

from fqdb import config, root_safety
from fqdb.config import Config, DataRootUnavailable, select_root


def test_unavailable_saved_root_does_not_create_a_fallback(empty_data_root, monkeypatch):
    root = empty_data_root / "SYNTHETIC-selected"
    select_root(root)
    original = root.rename(empty_data_root / "SYNTHETIC-original-storage")
    monkeypatch.setattr(config, "_current_root", None)
    for shadow_directory in (False, True):
        if shadow_directory:
            root.mkdir()
        with pytest.raises(DataRootUnavailable, match="unavailable"):
            Config().ensure()
        with pytest.raises(DataRootUnavailable, match="unavailable"):
            Config(root).ensure()
        assert root.exists() == shadow_directory
        assert not (root / ".state").exists()
        assert not (empty_data_root / "SYNTHETIC-home/.fqdb").exists()
    assert (original / ".state/config.json").is_file()


def test_environment_override_remembers_the_device_without_changing_default(empty_data_root, monkeypatch):
    selected = select_root(empty_data_root / "SYNTHETIC-saved")
    override = empty_data_root / "SYNTHETIC-environment"
    monkeypatch.setattr(config, "_current_root", None)
    monkeypatch.setenv("FQDB_DATA_DIR", str(override))
    active = Config().ensure()
    active.remember()
    assert active.root == override
    assert config.saved_root() == selected
    override.rename(empty_data_root / "SYNTHETIC-original-environment")
    override.mkdir()
    with pytest.raises(DataRootUnavailable, match="recorded data root marker is missing"):
        Config().ensure()
    assert not list(override.iterdir())
    monkeypatch.delenv("FQDB_DATA_DIR")
    assert Config().ensure().root == selected


def test_same_root_marker_requires_the_original_mount(empty_data_root, monkeypatch):
    root = empty_data_root / "SYNTHETIC-mounted"
    expected = {"location": str(root.parent), "root": "/", "filesystem": "SYNTHETIC"}
    monkeypatch.setattr(config, "mount_info", lambda *_: expected)
    active = Config(root).ensure()
    active.remember(select=True)
    original = active.path.read_bytes()
    fallback = {"location": "/", "root": "/", "filesystem": "SYNTHETIC"}
    monkeypatch.setattr(config, "mount_info", lambda *_: fallback)
    with pytest.raises(DataRootUnavailable, match="recorded storage"):
        Config(root).ensure()
    assert active.path.read_bytes() == original


def test_runtime_guard_blocks_writes_when_the_path_is_replaced(empty_data_root):
    root = empty_data_root / "SYNTHETIC-active"
    active = Config(root).ensure()
    active.remember()
    with active.pin():
        original = root.rename(empty_data_root / "SYNTHETIC-original-active")
        root.mkdir()
        with pytest.raises(DataRootUnavailable, match="unavailable"):
            active.check_available()
        with pytest.raises(DataRootUnavailable, match="unavailable"):
            active.update(upload_limit=200_000)
    assert not list(root.iterdir())
    assert json.loads((original / ".state/config.json").read_text())["upload_limit"] == 500_000


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Configured Linux mount detection uses fstab"
)
def test_first_use_rejects_configured_mount_without_its_disk(empty_data_root, monkeypatch):
    mount = empty_data_root / "SYNTHETIC-missing-mount"
    mount.mkdir()
    root = mount / "fqdb"
    monkeypatch.setattr(root_safety, "_configured_mounts", lambda: [mount])
    with pytest.raises(DataRootUnavailable, match="not mounted"):
        Config(root).ensure()
    assert not root.exists()


@pytest.mark.skipif(os.name == "nt", reason="Creating Windows symlinks can require administrator privileges")
def test_recorded_path_cannot_be_redirected_by_symlink(empty_data_root):
    original = select_root(empty_data_root / "SYNTHETIC-selected")
    alternate = Config(empty_data_root / "SYNTHETIC-other").ensure().root
    original.rename(empty_data_root / "SYNTHETIC-parked")
    original.symlink_to(alternate, target_is_directory=True)
    with pytest.raises(DataRootUnavailable, match="not a directory"):
        Config(original).ensure()


@pytest.mark.skipif(os.name == "nt", reason="Creating Windows symlinks can require administrator privileges")
def test_existing_storage_alias_shares_the_root_and_rejects_retargeting(empty_data_root, monkeypatch):
    original = empty_data_root / "SYNTHETIC-original"
    original.mkdir()
    alias = empty_data_root / "SYNTHETIC-alias"
    alias.symlink_to(original, target_is_directory=True)
    monkeypatch.setenv("FQDB_DATA_DIR", str(alias))
    env = Config().ensure()
    env.remember()
    assert env.root == Config(original).ensure().root
    assert Config().ensure().root == original

    replacement = empty_data_root / "SYNTHETIC-replacement"
    alias.unlink()
    alias.symlink_to(replacement, target_is_directory=True)
    with pytest.raises(DataRootUnavailable, match="recorded directory alias"):
        Config().ensure()
    assert not replacement.exists()


def test_legacy_saved_path_must_already_be_initialized(empty_data_root):
    root = empty_data_root / "SYNTHETIC-existing"
    root.mkdir()
    config.user_config_path().write_text(json.dumps({"schema_version": 1, "data_dir": str(root)}))
    with pytest.raises(DataRootUnavailable, match="configured data layout is missing"):
        Config().ensure()
    assert not list(root.iterdir())


def test_new_upload_default_does_not_replace_saved_limit(empty_data_root):
    root = empty_data_root / "SYNTHETIC-limits"
    active = Config(root).ensure()
    assert active.read()["upload_limit"] == 500_000
    active.update(upload_limit=1_000_000)
    assert Config(root).ensure().read()["upload_limit"] == 1_000_000
