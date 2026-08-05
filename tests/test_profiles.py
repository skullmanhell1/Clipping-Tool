"""Tests for saved settings profiles."""

from __future__ import annotations

from profiles import ProfileStore


def test_create_and_list(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    p = store.save("Shorts", {"aspect": "9:16", "emoji": "heavy"}, {"mode": "review"})
    assert p.id
    listed = store.list()
    assert len(listed) == 1
    assert listed[0].settings["aspect"] == "9:16"


def test_update_existing(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    p = store.save("A", {"aspect": "9:16"}, {})
    updated = store.save("A renamed", {"aspect": "1:1"}, {}, profile_id=p.id)
    assert updated.id == p.id
    assert len(store.list()) == 1
    assert store.get(p.id).settings["aspect"] == "1:1"
    assert store.get(p.id).name == "A renamed"


def test_default_is_exclusive_and_persisted(tmp_path):
    path = tmp_path / "p.json"
    store = ProfileStore(path)
    a = store.save("A", {}, {}, make_default=True)
    b = store.save("B", {}, {})
    store.set_default(b.id)
    assert store.get_default().id == b.id
    assert store.get(a.id).is_default is False
    # Persisted across reloads.
    reloaded = ProfileStore(path)
    assert reloaded.get_default().id == b.id


def test_delete(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    p = store.save("A", {}, {})
    assert store.delete(p.id) is True
    assert store.get(p.id) is None
    assert store.delete("missing") is False
