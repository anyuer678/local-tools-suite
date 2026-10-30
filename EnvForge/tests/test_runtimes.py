from __future__ import annotations

import os
import sys

import pytest

from envforge.errors import NotFoundError, ValidationError
from envforge.models import RuntimeEntry
from envforge.runtimes import RuntimeService, build_activation_script


def _fake_which(mapping):
    return lambda name: mapping.get(name)


def _fake_probe(mapping):
    return lambda path, kind: (mapping.get(path, ""), "")


@pytest.fixture
def service(db, tmp_path):
    py = tmp_path / "Python312" / "python.exe"
    node = tmp_path / "nodejs" / "node.exe"
    java = tmp_path / "Zulu" / "bin" / "java.exe"
    for exe in (py, node, java):
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("stub", encoding="utf-8")
    which_map = {
        "python.exe": str(py),
        "python": str(py),
        "node.exe": str(node),
        "java.exe": str(java),
    }
    probe_map = {str(py): "3.12.3", str(node): "20.11.0", str(java): "21.0.2"}
    return RuntimeService(db, _fake_which(which_map), _fake_probe(probe_map))


def test_detect_dedupes_same_realpath(service, db):
    found = service.detect()
    kinds = sorted(item.kind for item in found)
    assert kinds == ["java", "node", "python"]
    assert {item.version for item in found} == {"3.12.3", "20.11.0", "21.0.2"}


def test_detect_invalid_kind(service):
    with pytest.raises(ValidationError):
        service.detect(["ruby"])


def test_detect_skips_unprobeable_stubs(db, tmp_path):
    stub = tmp_path / "WindowsApps" / "python3.exe"
    stub.parent.mkdir(parents=True)
    stub.write_text("stub", encoding="utf-8")
    service = RuntimeService(
        db,
        lambda name: str(stub) if name == "python3.exe" else None,
        lambda path, kind: ("", ""),
    )
    assert service.detect(["python"]) == []


def test_detect_and_register_counts(service, db):
    result = service.detect_and_register()
    assert result["added"] == 3 and result["updated"] == 0
    again = service.detect_and_register()
    assert again["added"] == 0 and again["updated"] == 3
    assert len(service.list()) == 3


def test_register_requires_existing_file(service, tmp_path):
    with pytest.raises(ValidationError):
        service.register("python", str(tmp_path / "missing.exe"))
    with pytest.raises(ValidationError):
        service.register("python", str(tmp_path))


def test_register_upserts_version(service, tmp_path):
    exe = tmp_path / "python.exe"
    exe.write_text("stub", encoding="utf-8")
    first = service.register("python", str(exe), "3.9.0")
    second = service.register("python", str(exe), None)
    assert first.id == second.id
    assert second.version == "3.9.0"  # 未提供版本且探测为空时保留旧值

    with pytest.raises(ValidationError):
        service.register("ruby", str(exe))


def test_default_entries_picks_highest_version(service, tmp_path):
    exe_old = tmp_path / "py39.exe"
    exe_new = tmp_path / "py313.exe"
    for exe in (exe_old, exe_new):
        exe.write_text("stub", encoding="utf-8")
    service.register("python", str(exe_old), "3.9.12")
    service.register("python", str(exe_new), "3.13.0")
    defaults = {entry.kind: entry for entry in service.default_entries()}
    assert defaults["python"].version == "3.13.0"


def test_remove_missing(service):
    with pytest.raises(NotFoundError):
        service.remove(999)


def test_list_filter_and_kind_validation(service):
    service.detect_and_register(["node"])
    assert [item.kind for item in service.list("node")] == ["node"]
    with pytest.raises(ValidationError):
        service.list("ruby")


def test_activation_script_quotes_and_prefixes(tmp_path):
    exe = tmp_path / "it's" / "python.exe"
    exe.parent.mkdir()
    exe.write_text("stub", encoding="utf-8")
    entry = RuntimeEntry(kind="python", version="3.12.3", exe_path=str(exe))
    script = build_activation_script(
        [entry], {"APP_ENV": "dev", "QUOTED": "a'b"}, project_path=r"C:\work\demo"
    )
    assert "$env:PATH = '" in script
    assert "it''s" in script
    assert "$env:APP_ENV = 'dev'" in script
    assert "$env:QUOTED = 'a''b'" in script
    assert "$env:ENVFORGE_PROJECT = 'C:\\work\\demo'" in script
    assert "python 3.12.3" in script


def test_activation_script_without_entries():
    script = build_activation_script([])
    assert script.startswith("# EnvForge 激活脚本")
    assert "$env:PATH" not in script


def test_service_activation_script_uses_defaults(service):
    service.detect_and_register()
    script = service.activation_script(project_path=r"C:\p")
    assert "Python312" in script
    assert "nodejs" in script
