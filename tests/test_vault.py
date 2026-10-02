"""Obsidian mirror: writes only its own folder and only its own files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm import vault

SNAP = {
    "load": {"lines": ["mode: full", "  - nothing competing", "signals: CPU 5%"]},
    "machine": {
        "gpus": [{"name": "GPU", "temp_c": 50.0, "power_w": 60.0, "power_limit_w": 245.0, "mem_used_mb": 1800.0,
                  "mem_total_mb": 8192.0}],
        "disks": [{"name": "SSD", "size_gb": 932, "health": "Healthy"}],
        "volumes": [{"mount": "C:\\", "free_gb": 77, "total_gb": 499, "used_pct": 84.6}],
    },
    "local": {"calls": 3, "tokens_in": 1000, "tokens_out": 200, "seconds": 40.0, "kwh": 0.001,
              "electricity_usd": 0.0003, "modes": {"full": 3}},
    "claude": {"since_days": 14, "by_day": [{"day": "2026-10-01", "output": 1234, "input": 5, "cache_read": 9,
                                              "messages": 2}],
               "by_project": [{"project": "D--Code-swarm", "output": 1234}]},
}  # fmt: skip


def test_sync_writes_mirror_and_home_note(tmp_path: Path) -> None:
    src = tmp_path / "docs"
    src.mkdir()
    (src / "operating-model.md").write_text("# Operating model\n\nbody\n", encoding="utf-8")
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "Welcome.md").write_text("mine", encoding="utf-8")

    result = vault.sync(vault_dir, [src / "operating-model.md"], SNAP)
    folder = vault_dir / "Swarm"
    note = (folder / "operating-model.md").read_text(encoding="utf-8")
    assert note.startswith("---\nswarm_mirror: true\n") and "# Operating model" in note
    home = (folder / "Swarm Home.md").read_text(encoding="utf-8")
    assert "[[operating-model]]: Operating model" in home and "[[Swarm Status]]" in home
    status = (folder / "Swarm Status.md").read_text(encoding="utf-8")
    assert "1,234" in status and "Healthy" in status
    assert sorted(result["written"]) == ["Swarm Home.md", "Swarm Status.md", "operating-model.md"]
    assert (vault_dir / "Welcome.md").read_text(encoding="utf-8") == "mine"  # nothing outside the folder is touched


def test_sync_never_overwrites_or_deletes_the_owners_notes(tmp_path: Path) -> None:
    src = tmp_path / "notes.md"
    src.write_text("# From swarm\n", encoding="utf-8")
    folder = tmp_path / "vault" / "Swarm"
    folder.mkdir(parents=True)
    (folder / "notes.md").write_text("my own note with the same name", encoding="utf-8")
    (folder / "my ideas.md").write_text("mine too", encoding="utf-8")
    (folder / "stale.md").write_text("---\nswarm_mirror: true\n---\nold mirror\n", encoding="utf-8")

    result = vault.sync(tmp_path / "vault", [src], None)
    assert (folder / "notes.md").read_text(encoding="utf-8") == "my own note with the same name"
    assert result["skipped"] == ["notes.md"]
    assert (folder / "my ideas.md").exists()  # not ours: kept
    assert not (folder / "stale.md").exists() and result["removed"] == ["stale.md"]  # ours and gone from source


def test_find_vault_prefers_env_then_the_open_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARM_OBSIDIAN_VAULT", str(tmp_path / "x"))
    assert vault.find_vault() == tmp_path / "x"

    monkeypatch.delenv("SWARM_OBSIDIAN_VAULT")
    old, current = tmp_path / "old", tmp_path / "current"
    old.mkdir()
    current.mkdir()
    registry = {"vaults": {"a": {"path": str(old), "ts": 9}, "b": {"path": str(current), "ts": 1, "open": True},
                           "c": {"path": str(tmp_path / "gone"), "ts": 99, "open": True}}}  # fmt: skip
    config = tmp_path / "appdata" / "obsidian"
    config.mkdir(parents=True)
    (config / "obsidian.json").write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(vault.os, "name", "nt")
    assert vault.find_vault() == current
