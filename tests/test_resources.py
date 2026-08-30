import sys
from pathlib import Path

import pytest

from agent import resources


def test_resource_path_in_development_is_relative_to_repo_root(monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    p = resources.resource_path("data/demo.db")
    assert p.name == "demo.db"
    assert p.parent.name == "data"
    # agent/resources.py lives one level below the repo root
    assert (p.parent.parent / "agent" / "resources.py").exists()


def test_resource_path_in_a_bundle_uses_meipass(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    p = resources.resource_path("agent/knowledge")
    assert p == tmp_path / "agent" / "knowledge"


def test_user_data_dir_is_created_and_writable(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    d = resources.user_data_dir()
    assert d.is_dir()
    assert d.name == resources.APP_NAME
    (d / "probe.txt").write_text("ok", encoding="utf-8")


def test_ensure_demo_db_copies_once_then_reuses(monkeypatch, tmp_path):
    bundled = tmp_path / "bundle" / "data"
    bundled.mkdir(parents=True)
    (bundled / "demo.db").write_bytes(b"ORIGINAL")
    monkeypatch.setattr(
        resources, "resource_path", lambda rel: tmp_path / "bundle" / rel
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))

    first = resources.ensure_demo_db()
    assert first.read_bytes() == b"ORIGINAL"
    assert first.parent != bundled, "must not hand back the read-only bundled copy"

    # a second call must not clobber local changes
    first.write_bytes(b"MODIFIED")
    second = resources.ensure_demo_db()
    assert second == first
    assert second.read_bytes() == b"MODIFIED"


def test_demo_db_url_is_a_sqlite_url(monkeypatch, tmp_path):
    monkeypatch.setattr(resources, "ensure_demo_db", lambda: tmp_path / "demo.db")
    assert resources.demo_db_url() == f"sqlite:///{tmp_path / 'demo.db'}"


def test_write_text_file_uses_utf8(tmp_path):
    target = tmp_path / "sub" / "report.md"
    resources.write_text_file(target, "דוח סיכון")
    assert target.read_text(encoding="utf-8") == "דוח סיכון"
