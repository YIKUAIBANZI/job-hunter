"""配置工具离线测试：配置均写临时目录，Chrome 与凭证读取均为替身。"""

import json
from copy import deepcopy
from pathlib import Path

import pytest

import config_tool as tool
from shared import DEFAULT_CONFIG, load_config
REAL_PROBE = tool.probe_chrome_port


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(tool, "probe_chrome_port", lambda: True)
    monkeypatch.setattr(tool, "load_jev_key", lambda: "")
    monkeypatch.setattr(tool.llm_match, "load_llm_key", lambda *a, **k: "")
    monkeypatch.setattr(tool.socket, "create_connection", lambda *a, **k: pytest.fail("禁止真实 TCP"))


def config(tmp_path, **updates):
    (tmp_path / "resume.md").write_text("测试简历内容", encoding="utf-8")
    return {**deepcopy(DEFAULT_CONFIG), "resume_path": "resume.md", "target_roles": ["运营"],
            "job_profile": "社招运营，深圳", "matcher": "keyword", **updates}


def store(tmp_path, cfg):
    (tmp_path / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def test_init_from_example_without_overwrite(tmp_path, capsys):
    example = config(tmp_path)
    (tmp_path / "config.example.json").write_text(json.dumps(example), encoding="utf-8")
    assert tool.main(["init", "--resume", "resume.md", "--city", "杭州", "--matcher", "keyword"], root=tmp_path) == 0
    path = tmp_path / "config.json"
    original = path.read_bytes()
    cfg = json.loads(original)
    assert cfg["city"] == "杭州" and cfg["matcher"] == "keyword"
    assert cfg["skills"] == example["skills"]
    assert tool.main(["init", "--resume", "other.md"], root=tmp_path) == 1
    assert path.read_bytes() == original
    assert "不覆盖；请用 set" in capsys.readouterr().out


@pytest.mark.parametrize("field,raw,expected", [
    ("target_roles", " 运营，电商运营, 增长 ,, ", ["运营", "电商运营", "增长"]),
    ("skills", "Python，SQL", ["Python", "SQL"]),
    ("resume_redact", "张三", ["张三"]),
    ("exclude_keywords", "", []),
    ("min_score", "0", 0), ("min_score", "100", 100), ("default_count", "1", 1),
    ("matcher", "llm", "llm"), ("llm.model", " cheap-model ", "cheap-model"),
    ("llm.api_key_env", "MY_LLM_KEY", "MY_LLM_KEY"),
])
def test_parse_values(field, raw, expected):
    assert tool.parse_value(field, raw) == expected


@pytest.mark.parametrize("field,raw", [("min_score", "-1"), ("min_score", "101"), ("min_score", "1.5"),
                                      ("default_count", "0"), ("matcher", "unknown"), ("city", "  "),
                                      ("llm.api_key_env", "a-key"), ("llm.base_url", "https://key@example.com"),
                                      ("api_key", "secret"), ("llm.api_key", "secret"), ("unknown", "secret")])
def test_invalid_values_are_safe(field, raw):
    with pytest.raises(ValueError) as exc:
        tool.parse_value(field, raw)
    assert "secret" not in str(exc.value)
    if field not in tool.FIELD_INFO:
        assert "可用字段" in str(exc.value)


def test_set_is_pure_and_preserves_nested_fields():
    original = {"llm": {"base_url": "https://example.com/v1"}, "其他": ["中文"]}
    updated = tool.set_field(original, "llm.model", "cheap")
    assert updated["llm"] == {"base_url": "https://example.com/v1", "model": "cheap"}
    updated["其他"].append("新值")
    assert original == {"llm": {"base_url": "https://example.com/v1"}, "其他": ["中文"]}


@pytest.mark.parametrize("field,value", [("min_score", True), ("target_roles", "运营"), ("target_roles", [1]), ("llm", {"api_key": "secret"})])
def test_set_rejects_invalid_types(field, value):
    with pytest.raises(ValueError):
        tool.set_field({}, field, value)


def test_cli_set_atomic_preserves_chinese_and_other_fields(tmp_path, monkeypatch, capsys):
    cfg = config(tmp_path)
    cfg["其他"] = "中文"
    store(tmp_path, cfg)
    original_replace = Path.replace
    replacements = []

    def replace(source, target):
        assert source.parent == target.parent == tmp_path
        assert json.loads(target.read_text())["target_roles"] == ["运营"]
        replacements.append(source)
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace)
    assert tool.main(["set", "target_roles", "电商运营，增长"], root=tmp_path) == 0
    assert len(replacements) == 1 and not replacements[0].exists()
    raw = (tmp_path / "config.json").read_text()
    assert "中文" in raw and "\\u" not in raw
    assert json.loads(raw)["其他"] == "中文"
    assert "✅" in capsys.readouterr().out


def test_atomic_failure_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{"原值":1}')
    def fail(*a):
        raise OSError("写入失败")
    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        tool.atomic_write(path, {"新值": 2})
    assert path.read_text() == '{"原值":1}'
    assert list(tmp_path.glob(".config-*.tmp")) == []


def test_cli_set_unset_and_checks(tmp_path, capsys):
    store(tmp_path, config(tmp_path))
    for field, value in [("min_score", "75"), ("default_count", "4"), ("llm.model", "cheap"), ("city", "杭州")]:
        assert tool.main(["set", field, value], root=tmp_path) == 0
    assert load_config(tmp_path)["llm"]["api_key_env"] == "JOBHUNTER_LLM_KEY"
    assert tool.main(["unset", "llm.model"], root=tmp_path) == 0
    assert load_config(tmp_path)["llm"]["model"] == ""
    assert tool.main(["unset", "city"], root=tmp_path) == 0
    assert load_config(tmp_path)["city"] == "深圳"
    assert tool.main(["unset", "resume_path"], root=tmp_path) == 1
    assert tool.main(["set", "unknown", "secret"], root=tmp_path) == 1
    assert "可用字段" in capsys.readouterr().out
    # 写入完成即使 check 不通过也保留修改，并以 1 返回检查失败。
    assert tool.main(["set", "matcher", "llm"], root=tmp_path) == 1
    assert load_config(tmp_path)["matcher"] == "llm"


def test_check_items_and_default_profile(tmp_path):
    cfg = config(tmp_path)
    rows = tool.check_config(cfg, tmp_path, probe_chrome=lambda: True)
    assert all(r["ok"] for r in rows)
    assert len([r for r in rows if "仍为默认 2027 届 AI 产品画像" in r["message"]]) == 4
    assert any(r["field"] == "matcher" and "无需模型" in r["message"] for r in rows)
    before = deepcopy(cfg)
    cfg["jev_direction"] = "运营"
    rows = tool.check_config(cfg, tmp_path, probe_chrome=lambda: False)
    assert next(r for r in rows if r["field"] == "chrome")["ok"] is False
    assert "已自定义" in next(r for r in rows if r["field"] == "jev_direction")["message"]
    assert before["llm"] == cfg["llm"]


@pytest.mark.parametrize("update,field", [({"resume_path": "missing"}, "resume_path"), ({"resume_path": "empty.md"}, "resume_path"),
                                         ({"target_roles": []}, "target_roles"), ({"target_roles": "运营"}, "target_roles"),
                                         ({"job_profile": " "}, "job_profile"), ({"matcher": "bad"}, "matcher"),
                                         ({"default_count": 0}, "default_count"), ({"min_score": 101}, "min_score"),
                                         ({"llm": None, "matcher": "llm"}, "matcher"), ({"jev_direction": " "}, "jev_direction")])
def test_check_invalid_config(tmp_path, update, field):
    cfg = config(tmp_path, **update)
    (tmp_path / "empty.md").write_text(" \n")
    rows = tool.check_config(cfg, tmp_path, probe_chrome=None)
    assert any(r["field"] == field and not r["ok"] for r in rows)


@pytest.mark.parametrize("matcher", ["jev", "llm"])
def test_check_credentials_mocked(tmp_path, monkeypatch, matcher):
    cfg = config(tmp_path, matcher=matcher)
    cfg["llm"].update(base_url="https://example.com/v1", model="cheap")
    rows = tool.check_config(cfg, tmp_path, probe_chrome=None)
    assert not next(r for r in rows if r["field"] == "matcher")["ok"]
    monkeypatch.setattr(tool, "load_jev_key", lambda: "secret")
    monkeypatch.setattr(tool.llm_match, "load_llm_key", lambda *a: "secret")
    rows = tool.check_config(cfg, tmp_path, probe_chrome=None)
    assert all(r["ok"] for r in rows)
    assert "secret" not in str(rows)


def test_check_cli_no_chrome_and_exit_code(tmp_path, monkeypatch, capsys):
    store(tmp_path, config(tmp_path))
    monkeypatch.setattr(tool, "probe_chrome_port", lambda: pytest.fail("不能探测"))
    assert tool.main(["check", "--no-chrome"], root=tmp_path) == 0
    monkeypatch.setattr(tool, "probe_chrome_port", lambda: False)
    assert tool.main(["check"], root=tmp_path) == 1
    assert "❌ Chrome" in capsys.readouterr().out


def test_probe_only_tcp_with_mock(monkeypatch):
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *a): pass
    calls = []
    monkeypatch.setattr(tool.socket, "create_connection", lambda address, timeout: calls.append((address, timeout)) or Connection())
    assert REAL_PROBE()
    assert calls == [(("127.0.0.1", 9222), 1)]
    def fail(*a, **k):
        raise OSError("拒绝连接")
    monkeypatch.setattr(tool.socket, "create_connection", fail)
    assert not REAL_PROBE()


def test_show_and_fields_without_secret(tmp_path, monkeypatch, capsys):
    cfg = config(tmp_path)
    cfg["llm"]["api_key"] = "stored-secret"
    cfg["api_key"] = "stored-secret"
    store(tmp_path, cfg)
    monkeypatch.setattr(tool.llm_match, "load_llm_key", lambda *a: "env-secret")
    assert tool.main(["show"], root=tmp_path) == 0
    output = capsys.readouterr().out
    assert "已配置" in output and "env-secret" not in output and "stored-secret" not in output
    monkeypatch.setattr(tool.llm_match, "load_llm_key", lambda *a: "")
    assert "未配置" in json.dumps(tool.show_config({}), ensure_ascii=False)
    assert tool.main(["fields"], root=tmp_path) == 0
    output = capsys.readouterr().out
    assert all(field in output for field in tool.FIELD_INFO)
    assert set(DEFAULT_CONFIG) - {"llm"} <= set(tool.FIELD_INFO)


def test_load_config_defaults_are_not_mutated(tmp_path):
    store(tmp_path, {"llm": {"model": "cheap"}})
    cfg = load_config(tmp_path)
    cfg["resume_redact"].append("姓名")
    cfg["llm"]["base_url"] = "https://example.com"
    fresh = load_config(tmp_path)
    assert fresh["llm"] == {"base_url": "", "model": "cheap", "api_key_env": "JOBHUNTER_LLM_KEY"}
    assert fresh["resume_redact"] == [] and fresh["matcher"] == "jev"
