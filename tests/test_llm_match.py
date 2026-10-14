"""LLM 后端离线契约测试；全部 HTTP 请求均为替身。"""

import io
import json
from copy import deepcopy
from urllib.error import HTTPError

import pytest

import jev_match as jev
import llm_match as llm
from test_jev_match import answers

SETTINGS = {"base_url": "https://llm.example/v1/", "model": "cheap-model", "api_key_env": "TEST_LLM_KEY"}
KEY = "offline-secret-key"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(llm.request, "urlopen", lambda *a, **k: pytest.fail("禁止真实网络"))
    monkeypatch.setattr(jev, "BACKEND", "jev")
    monkeypatch.setattr(jev, "LLM_SETTINGS", deepcopy(llm.DEFAULT_SETTINGS))


def response(content):
    return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())


def full_answers():
    raw = answers()
    raw["fit"]["probabilities"] = {"0": 0, "1": 0, "2": 0, "3": 1}
    raw["action"]["probabilities"] = {"0": 1, "1": 0, "2": 0}
    return raw


@pytest.mark.parametrize("fenced", [False, True])
def test_request_contract_and_decisions(monkeypatch, fenced):
    raw = full_answers()
    content = json.dumps(raw)
    if fenced:
        content = "```json\n" + content + "\n```"

    def open_request(req, timeout):
        assert req.full_url == "https://llm.example/v1/chat/completions"
        assert req.get_method() == "POST" and timeout == 7
        assert req.get_header("Authorization") == f"Bearer {KEY}"
        body = json.loads(req.data)
        assert body["temperature"] == 0 and body["model"] == SETTINGS["model"]
        assert body["response_format"] == {"type": "json_object"}
        user = json.loads(body["messages"][1]["content"])
        assert user == {"state": "脱敏资料", "questions": jev.QUESTIONS}
        assert "满足/是" in body["messages"][0]["content"]
        assert KEY not in str(body)
        return response(content)

    monkeypatch.setattr(llm.request, "urlopen", open_request)
    result = llm.request_answers("脱敏资料", jev.QUESTIONS, SETTINGS, KEY, 7)
    assert jev.decide(result)["decision"] == "apply"


def test_normalizes_distributions_and_accepts_title():
    raw = full_answers()
    raw["fit"]["probabilities"] = {"0": .1, "1": .1, "2": .1, "3": .6}
    raw["action"]["probabilities"] = {"0": .8, "1": .1, "2": .05}
    result = llm.parse_answers(json.dumps(raw), jev.QUESTIONS)
    assert sum(result["fit"]["probabilities"].values()) == pytest.approx(1)
    assert result["fit"]["probabilities"]["3"] == pytest.approx(2/3)
    jev.decide(result)
    title = llm.parse_answers('```json\n{"title_fit":{"choice":"1","probabilities":{"0":0.05,"1":0.9}}}\n```', jev.TITLE_QUESTIONS)
    assert jev.decide_title(title["title_fit"])[0]


@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("stage"),
    lambda r: r["edu"].update(noul=True),
    lambda r: r["edu"].update(noul=-.1),
    lambda r: r["edu"].update(noul=float("nan")),
    lambda r: r["action"].update(choice="4"),
    lambda r: r["action"].update(choice=0),
    lambda r: r["action"]["probabilities"].pop("2"),
    lambda r: r["fit"].update(probabilities={"0": 0, "1": 0, "2": 0, "3": 0}),
    lambda r: r["fit"].update(probabilities={"0": 0, "1": 0, "2": 0, "3": 1.2}),
    lambda r: r["fit"].update(probabilities={"0": 0, "1": 0, "2": 0, "4": 1}),
])
def test_invalid_answer_fails_closed(mutate):
    raw = full_answers()
    mutate(raw)
    with pytest.raises(jev.JevError, match="LLM 响应格式错误"):
        llm.parse_answers(json.dumps(raw), jev.QUESTIONS)


@pytest.mark.parametrize("content", [None, [], "private-response", "[]", "null", '```json\n{}', '{"edu": null}'])
def test_invalid_content_does_not_leak(content):
    with pytest.raises(jev.JevError) as exc:
        llm.parse_answers(content, jev.QUESTIONS)
    assert "private-response" not in str(exc.value)


def test_http_400_retries_only_without_response_format(monkeypatch):
    bodies = []

    def open_request(req, timeout):
        bodies.append(json.loads(req.data))
        if len(bodies) == 1:
            raise HTTPError(req.full_url, 400, KEY, {}, None)
        return response(json.dumps(full_answers()))

    monkeypatch.setattr(llm.request, "urlopen", open_request)
    llm.request_answers("private-state", jev.QUESTIONS, SETTINGS, KEY)
    assert len(bodies) == 2
    assert bodies[0].pop("response_format") == {"type": "json_object"}
    assert bodies[0] == bodies[1]


@pytest.mark.parametrize("code,attempts", [(400, 2), (401, 1), (429, 1), (500, 1)])
def test_http_errors_are_safe_and_bounded(monkeypatch, code, attempts):
    calls = []

    def fail(req, **kw):
        calls.append(req)
        raise HTTPError(req.full_url, code, "private-state " + KEY, {}, io.BytesIO(b"private-response"))

    monkeypatch.setattr(llm.request, "urlopen", fail)
    with pytest.raises(jev.JevError) as exc:
        llm.request_answers("private-state", jev.QUESTIONS, SETTINGS, KEY)
    assert len(calls) == attempts
    assert all(secret not in str(exc.value) for secret in (KEY, "private-state", "private-response"))


@pytest.mark.parametrize("body", [b"private-response", b"{}", b'{"choices": []}', b'{"choices": [{}]}', b'{"choices":[{"message":{"content":null}}]}'])
def test_bad_openai_envelope(monkeypatch, body):
    monkeypatch.setattr(llm.request, "urlopen", lambda *a, **k: io.BytesIO(body))
    with pytest.raises(jev.JevError) as exc:
        llm.request_answers("private-state", jev.QUESTIONS, SETTINGS, KEY)
    assert all(secret not in str(exc.value) for secret in (KEY, "private-state", "private-response"))


def test_network_error_does_not_leak(monkeypatch):
    def fail(*a, **k):
        raise OSError(KEY + " private-state")
    monkeypatch.setattr(llm.request, "urlopen", fail)
    with pytest.raises(jev.JevError) as exc:
        llm.request_answers("private-state", jev.QUESTIONS, SETTINGS, KEY)
    assert str(exc.value) == "LLM 网络或解析失败"


def test_key_env_then_file(monkeypatch, tmp_path):
    path = tmp_path / "llm.env"
    path.write_text('# 注释\nOTHER_KEY=no\n TEST_LLM_KEY = "file-key"\n', encoding="utf-8")
    monkeypatch.setenv("TEST_LLM_KEY", "env-key")
    assert llm.load_llm_key(SETTINGS, path) == "env-key"
    monkeypatch.delenv("TEST_LLM_KEY")
    assert llm.load_llm_key(SETTINGS, path) == "file-key"
    assert llm.load_llm_key(SETTINGS, tmp_path / "missing") == ""
    monkeypatch.setattr(llm.Path, "expanduser", lambda p: path)
    assert llm.load_llm_key(SETTINGS) == "file-key"


@pytest.mark.parametrize("url", ["", None, [], "ftp://example.com", "https://key@example.com/v1", "https://example.com?key=secret", "https://example.com#key"])
def test_invalid_settings_prevent_request(url):
    with pytest.raises(jev.JevError, match="config_tool.py check"):
        llm.request_answers("state", jev.QUESTIONS, {**SETTINGS, "base_url": url}, KEY)


def test_numeric_string_probabilities_accepted():
    # 2026-10-14 实测 LongCat-2.0 输出 {"noul": "1"}、probabilities 值为字符串
    from jev_match import QUESTIONS, decide
    import json
    content = json.dumps({"edu": {"noul": "1"}, "stage": {"noul": "0.9"}, "schedule": {"noul": "1"},
                          "fit": {"probabilities": {"0": "0", "1": "0", "2": "0.2", "3": "0.8"}},
                          "action": {"probabilities": {"0": "1", "1": "0", "2": "0"}, "choice": "0"}})
    parsed = llm.parse_answers(content, QUESTIONS)
    assert parsed["stage"]["noul"] == 0.9 and decide(parsed)["decision"] == "apply"
    with pytest.raises(llm.JevError):
        llm.parse_answers(json.dumps({"edu": {"noul": "高"}}), {"edu": QUESTIONS["edu"]})
