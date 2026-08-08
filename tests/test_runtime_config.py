"""Tests for the runtime-mutable config store."""

from __future__ import annotations

from runtime_config import RETENTION_CHOICES, RuntimeConfigStore


def test_defaults_seed_from_config(tmp_path):
    store = RuntimeConfigStore(tmp_path / "rc.json")
    cfg = store.get()
    assert cfg.retention_days in RETENTION_CHOICES
    assert isinstance(cfg.auto_delete_temp, bool)


def test_update_persists(tmp_path):
    path = tmp_path / "rc.json"
    store = RuntimeConfigStore(path)
    store.update(retention_days=14, auto_delete_temp=False, delete_local_after_publish=True)
    assert path.exists()
    # A fresh store reads the persisted values.
    reloaded = RuntimeConfigStore(path)
    cfg = reloaded.get()
    assert cfg.retention_days == 14
    assert cfg.auto_delete_temp is False
    assert cfg.delete_local_after_publish is True


def test_retention_days_clamped_to_choices(tmp_path):
    store = RuntimeConfigStore(tmp_path / "rc.json")
    store.update(retention_days=1000)
    assert store.get().retention_days == 90  # nearest allowed choice
    store.update(retention_days=0)  # keep forever is valid
    assert store.get().retention_days == 0


def test_unknown_keys_ignored(tmp_path):
    store = RuntimeConfigStore(tmp_path / "rc.json")
    store.update(bogus="x", retention_days=30)
    assert not hasattr(store.get(), "bogus")
    assert store.get().retention_days == 30
