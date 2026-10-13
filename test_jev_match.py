"""Jev 匹配单元测试：只用伪造的响应与临时文件，禁止联网。"""

import copy
import io
import json
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError

import pytest

import jev_match as jev
from shared import DEFAULT_CONFIG, load_log, load_config


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(jev.request, "urlopen", lambda *a, **k: pytest.fail("禁止真实网络请求"))


def answers(hard=0.69, fit=2.5, action="0", p=0.8):
    low = int(fit)
    probs = {str(i): 0.0 for i in range(4)}
    probs[str(low)] = 1 - (fit - low)
    if low < 3:
        probs[str(low + 1)] = fit - low
    return {
        "edu": {"type": "noul", "noul": hard},
        "stage": {"type": "noul", "noul": max(hard, 0.95)},
        "schedule": {"type": "noul", "noul": max(hard, 0.9)},
        "fit": {"type": "score", "score": 99, "probabilities": probs},
        "action": {"type": "choice", "choice": action, "confidence": 0.01,
                   "probabilities": {str(i): p if str(i) == action else (1 - p) / 2
                                     for i in range(3)}},
    }


def test_redact_resume_is_pure_and_preserves_other_content():
    text = "张三｜13812345678｜+86 19912345678｜a.b+jobs@example.com\nPython Agent 项目 2027届"
    names = ["张三", "", "张三"]
    original = copy.deepcopy(names)
    assert jev.redact_resume(text, names) == (
        "[已隐去]｜[已隐去]｜+86 [已隐去]｜[已隐去]\nPython Agent 项目 2027届")
    assert names == original
    assert jev.redact_resume("项目编号 1138123456789、版本 1.11，Python") == "项目编号 1138123456789、版本 1.11，Python"


def test_redaction_custom_items_are_literal_and_longer_matches_first():
    assert jev.redact_resume("张三丰 A+B", ["张三", "张三丰", "A+B"]) == "[已隐去] [已隐去]"


def test_build_state_structure_redaction_and_no_truncation():
    jd = "产品职责" * 3000
    assert jev.build_state("产品实习", "测试公司", jd, "13812345678，做过 RAG", "深圳") == (
        f"【岗位】产品实习｜测试公司\n{jd}\n【候选人简历】\n[已隐去]，做过 RAG\n【求职偏好】\n深圳")


@pytest.mark.parametrize("hard,fit,action,p,decision,reason", [
    (0.01, 3, "0", 0.99, "skip", "硬条件冲突"),
    (0.199999, 0, "2", 0.99, "skip", "硬条件冲突"),  # a 优先于 b/c
    (0.2, 2.5, "0", 0.8, "review", "硬条件不确定（学历 0.20）"),
    (0.8, 0, "2", 0.6, "skip", "Jev 建议跳过"),  # b 优先于 c，含边界
    (0.8, 3, "2", 0.599999, "review", "Jev 倾向跳过"),  # 跳过但把握不足 → 复核
    (0.8, 1.499999, "0", 0.8, "skip", "方向不匹配"),
    (0.8, 1.5, "0", 0.8, "review", "能力匹配不足 1.50"),
    (0.8, 2.299999, "0", 0.9, "review", "能力匹配不足"),
    (0.6, 2.3, "0", 0.3, "apply", "匹配达到推荐阈值"),  # 方案 B：不看 action 概率
    (0.71, 2.72, "0", 0.52, "apply", "匹配达到推荐阈值"),  # 实测英才达
    (0.62, 2.35, "1", 0.56, "apply", "匹配达到推荐阈值"),  # 实测彩讯：建议复核也可投
    (1, 3, "0", 1, "apply", "匹配达到推荐阈值"),
    (0.599999, 3, "0", 0.8, "review", "硬条件不确定"),
    (0.45, 1.7, "1", 0.4, "review", "硬条件不确定（学历 0.45）"),
])
def test_decide_rules_and_boundaries(hard, fit, action, p, decision, reason):
    raw = answers(hard, fit, action, p)
    before = copy.deepcopy(raw)
    result = jev.decide(raw)
    assert result["decision"] == decision
    assert reason in result["reason"]
    assert result["fit_ev"] == pytest.approx(fit)  # score=99 不参与决策
    assert result["hard_ok"] == hard
    assert result["action"] == action
    assert result["action_p"] == p  # confidence=0.01 不参与决策
    assert raw == before


def test_review_reason_lists_every_missing_condition():
    result = jev.decide(answers(0.45, 1.7, "1", 0.4))
    for reason in ("硬条件不确定（学历 0.45）", "能力匹配不足 1.70"):
        assert reason in result["reason"]


@pytest.mark.parametrize("field,value", [
    ("edu", {"noul": float("nan")}),
    ("stage", {"noul": -0.1}),
    ("schedule", {"noul": True}),
    ("fit", {"score": 3}),
    ("fit", {"probabilities": {"3": 0.01}}),
    ("fit", {"probabilities": {"4": 1}}),
    ("action", {"choice": "other", "probabilities": {"0": 1}}),
])
def test_invalid_answers_cannot_apply(field, value):
    raw = answers()
    raw[field] = value
    with pytest.raises(jev.JevError):
        jev.decide(raw)


def test_call_jev_request_and_raw_answers(monkeypatch):
    raw = answers()

    def urlopen(req, timeout):
        assert req.full_url == "https://api.typesafe.ai/v1/systemone"
        assert req.method == "POST"
        assert req.get_header("Authorization") == "Bearer test-only-key"
        assert req.get_header("Content-type") == "application/json"
        payload = json.loads(req.data)
        assert set(payload) == {"model", "state", "questions"}
        assert payload["model"] == "jev-latest"
        assert payload["state"] == "脱敏简历 × JD"
        assert payload["questions"] == jev.QUESTIONS
        assert {k: v["type"] for k, v in payload["questions"].items()} == {
            "edu": "noul", "stage": "noul", "schedule": "noul",
            "fit": "score", "action": "choice"}
        assert len(payload["questions"]["fit"]["criteria"]) == 4
        assert set(payload["questions"]["action"]["criteria"]) == {"0", "1", "2"}
        assert timeout == 7
        return io.BytesIO(json.dumps({"answers": raw, "model": "jev-1.13.0"}).encode())

    monkeypatch.setattr(jev.request, "urlopen", urlopen)
    assert jev.call_jev("脱敏简历 × JD", "test-only-key", timeout=7) == raw


@pytest.mark.parametrize("error", [
    HTTPError(jev.JEV_URL, 401, "secret-in-error", {}, None),
    URLError("secret-in-error"), TimeoutError("secret-in-error"),
])
def test_call_jev_errors_are_safe(monkeypatch, error):
    def urlopen(*a, **k):
        raise error
    monkeypatch.setattr(jev.request, "urlopen", urlopen)
    with pytest.raises(jev.JevError) as exc:
        jev.call_jev("sensitive-state", "test-only-key")
    for secret in ("secret-in-error", "sensitive-state", "test-only-key"):
        assert secret not in str(exc.value)


@pytest.mark.parametrize("body", [b"broken", b"\xff", b"null", b"[]", b"{}", b'{"answers": {}}'])
def test_call_jev_parse_errors(monkeypatch, body):
    monkeypatch.setattr(jev.request, "urlopen", lambda *a, **k: io.BytesIO(body))
    with pytest.raises(jev.JevError):
        jev.call_jev("state", "test-only-key")


def test_cache_hit_and_changed_jd_miss(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(jev, "call_jev", lambda *a: calls.append(a) or answers())
    path = tmp_path / "jev-cache.json"
    first = jev.match_job("job1", "职责一", "private-state", "private-key", path)
    assert jev.match_job("job1", "职责一", "private-state", "private-key", path) == first
    assert len(calls) == 1
    jev.match_job("job1", "职责二", "private-state", "private-key", path)
    jev.match_job("job2", "职责一", "private-state", "private-key", path)
    assert len(calls) == 3
    cache = json.loads(path.read_text())
    assert len(cache) == 5  # 三份 JD 缓存与两个 latest 索引
    assert cache["latest:job1"]["decision"] == first["decision"]
    assert set(first) == {"answers", "decision", "ts"}
    assert len(jev.cache_key("job1", "职责一").split(":")[-1]) == 12
    assert "private-" not in path.read_text()


def test_invalid_cache_is_replaced(monkeypatch, tmp_path):
    monkeypatch.setattr(jev, "call_jev", lambda *a: answers())
    path = tmp_path / "jev-cache.json"
    path.write_text("{invalid")
    assert jev.match_job("1", "JD", "state", "key", path)["decision"]["decision"] == "apply"
    path.write_text(json.dumps({jev.cache_key("1", "JD"): {"answers": {}}}))
    assert jev.match_job("1", "JD", "state", "key", path)["answers"] == answers()


@pytest.mark.parametrize("choice,p,skip", [
    ("1", 0.8, True), ("1", 0.99, True), ("1", 0.799999, False),
    ("0", 1.0, False), ("0", 0.8, False),
])
def test_decide_title(choice, p, skip):
    answer = {"type": "choice", "choice": choice, "confidence": 0.99,
              "probabilities": {choice: p, "0" if choice == "1" else "1": 1 - p}}
    before = copy.deepcopy(answer)
    assert jev.decide_title(answer) == (skip, f"标题粗筛无关 p={p:.2f}" if skip else "")
    assert answer == before


@pytest.mark.parametrize("answer", [
    None, [], {}, {"choice": "1"},
    {"choice": "2", "probabilities": {"0": 1}},
    {"choice": "1", "probabilities": {"0": 1}},
    {"choice": [], "probabilities": {"0": 1}},
    {"choice": "1", "probabilities": {"1": "0.8", "0": 0.2}},
    {"choice": "1", "probabilities": {"1": True}},
    {"choice": "1", "probabilities": {"1": float("nan")}},
    {"choice": "1", "probabilities": {"1": 1.2, "0": -0.2}},
    {"choice": "1", "probabilities": {"1": 0.8}},
    {"type": "score", "choice": "1", "probabilities": {"1": 1}},
])
def test_decide_title_invalid(answer):
    with pytest.raises(jev.JevError):
        jev.decide_title(answer)


def test_call_title_jev_contract(monkeypatch):
    answer = {"type": "choice", "choice": "1", "probabilities": {"1": 0.9, "0": 0.1}}

    def urlopen(req, timeout):
        payload = json.loads(req.data)
        assert payload["state"] == "岗位卡片"
        assert payload["questions"] == jev.TITLE_QUESTIONS
        assert set(payload["questions"]) == {"title_fit"}
        assert payload["questions"]["title_fit"]["type"] == "choice"
        assert set(payload["questions"]["title_fit"]["criteria"]) == {"0", "1"}
        assert timeout == 5
        return io.BytesIO(json.dumps({"answers": {"title_fit": answer}}).encode())

    monkeypatch.setattr(jev.request, "urlopen", urlopen)
    assert jev.call_title_jev("岗位卡片", "offline-key", timeout=5) == answer


@pytest.mark.parametrize("body", [b"broken", b"null", b'{"answers": []}',
                                  b'{"answers": {}}', b'{"answers": {"title_fit": {}}}'])
def test_call_title_jev_invalid(monkeypatch, body):
    monkeypatch.setattr(jev.request, "urlopen", lambda *a, **k: io.BytesIO(body))
    with pytest.raises(jev.JevError):
        jev.call_title_jev("state", "key")


def test_title_cache_hit_refresh_and_invalid_entry(monkeypatch, tmp_path):
    answer = {"choice": "1", "probabilities": {"1": 0.9, "0": 0.1}}
    calls = []
    monkeypatch.setattr(jev, "call_title_jev", lambda *a: calls.append(a) or answer)
    path = tmp_path / "jev-cache.json"
    for _ in range(2):
        assert jev.match_title("a", "private-state", "private-key", path) == (True, "标题粗筛无关 p=0.90")
    assert len(calls) == 1
    assert json.loads(path.read_text())["title:a"]["answer"] == answer
    assert "private-" not in path.read_text()
    jev.match_title("a", "state", "key", path, refresh=True)
    assert len(calls) == 2
    path.write_text(json.dumps({"title:a": {"answer": {}}}))
    jev.match_title("a", "state", "key", path)
    assert len(calls) == 3


def test_match_job_writes_latest_on_new_and_cached_jd(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(jev, "call_jev", lambda *a: calls.append(a) or answers())
    path = tmp_path / "jev-cache.json"
    first = jev.match_job("a", "JD", "state", "key", path)
    assert json.loads(path.read_text())["latest:a"] == {"decision": first["decision"], "ts": first["ts"]}
    cache = json.loads(path.read_text())
    del cache["latest:a"]  # 旧版缓存也能在下次读 JD 后补索引
    path.write_text(json.dumps(cache))
    assert jev.match_job("a", "JD", "state", "key", path) == first
    assert json.loads(path.read_text())["latest:a"]["decision"] == first["decision"]
    assert len(calls) == 1
    jev.match_job("a", "JD", "state", "key", path, refresh=True)
    assert len(calls) == 2


@pytest.mark.parametrize("age,hit", [
    (timedelta(0), True), (timedelta(days=7, microseconds=-1), True),
    (timedelta(days=7), False), (timedelta(days=8), False), (timedelta(seconds=-1), False),
])
def test_latest_match_age(tmp_path, age, hit):
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    cached = {"decision": jev.decide(answers(hard=0.45)), "ts": (now - age).isoformat()}
    path = tmp_path / "jev-cache.json"
    path.write_text(json.dumps({"latest:a": cached}))
    original = path.read_text()
    assert jev.latest_match("a", path, now) == (cached if hit else None)
    assert jev.latest_match("missing", path, now) is None
    assert path.read_text() == original


@pytest.mark.parametrize("entry", [None, {}, {"ts": "bad", "decision": {}},
                                    {"ts": "2026-10-14T00:00:00+00:00", "decision": {}}])
def test_latest_match_invalid(tmp_path, entry):
    path = tmp_path / "jev-cache.json"
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert jev.latest_match("a", path, now) is None
    path.write_text(json.dumps({"latest:a": entry}))
    assert jev.latest_match("a", path, now) is None


def test_load_key_env_precedes_file(monkeypatch, tmp_path, capsys):
    path = tmp_path / "jev.env"
    path.write_text("# comment\nJEV_API_KEY='file-only-key'\n")
    monkeypatch.setenv("JEV_API_KEY", "env-only-key")
    assert jev.load_jev_key(path) == "env-only-key"
    monkeypatch.delenv("JEV_API_KEY")
    assert jev.load_jev_key(path) == "file-only-key"
    assert jev.load_jev_key(tmp_path / "missing") == ""
    assert capsys.readouterr().out == ""


def test_load_key_default_home_file(monkeypatch, tmp_path):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    path = tmp_path / ".config/jobhunter/jev.env"
    path.parent.mkdir(parents=True)
    path.write_text("JEV_API_KEY=home-only-key\n")
    def expanduser(source):
        assert str(source) == "~/.config/jobhunter/jev.env"
        return path
    monkeypatch.setattr(jev.Path, "expanduser", expanduser)
    assert jev.load_jev_key() == "home-only-key"


def test_config_defaults_and_review_bucket(tmp_path):
    assert DEFAULT_CONFIG["job_profile"] == ""
    assert DEFAULT_CONFIG["resume_redact"] == []
    assert load_config(tmp_path)["resume_redact"] == []
    path = tmp_path / "boss-x-log.json"
    path.write_text(json.dumps({"review": [{"job_id": "1"}]}))
    assert load_log(path)["review"] == [{"job_id": "1"}]


def test_render_queue_tables_and_purity():
    review = {"company": "复核公司", "job": "产品实习", "salary": "200/天", "decision": "review",
              "jev": {"decision": jev.decide(answers(hard=0.45))},
              "reason": "硬条件不确定（学历 0.45）", "url": "https://www.zhipin.com/job_detail/1.html",
              "keyword_score": 85}
    apply = {**review, "company": "推荐公司", "decision": "apply", "reason": "dry-run 未沟通"}
    rows = [review, apply, {"company": "跳过公司", "job": "销售", "decision": "skip", "reason": "方向不匹配"},
            {**apply, "company": "已投公司", "attempted": True},
            {**apply, "company": "待核验公司", "status": "pending", "attempted": True}]
    stats = {"city": "深圳", "apply": 3, "review": 1, "skip": 1, "applied": 1, "pending": 1, "dry_run": True}
    before = copy.deepcopy((rows, stats))
    md = jev.render_queue_md(rows, stats)
    for text in ("推荐：3", "复核：1", "跳过：1", "已沟通：1", "待核验：1", "需要人工复核", "推荐但未投",
                 "本次跳过", "keyword 对照", "| 公司 | 岗位 | 薪资 | hard | fit | Jev 建议 | 原因 | 链接 |",
                 "复核公司", "推荐公司", "200/天", "0.45", "2.50", "方向不匹配", "| 85 |", review["url"]):
        assert text in md
    unsubmitted = md.split("## 推荐但未投")[1].split("## 本次跳过")[0]
    assert "已投公司" not in unsubmitted
    assert "待核验公司" not in unsubmitted
    assert (rows, stats) == before


def test_render_queue_skip_limit_and_escape():
    rows = [{"decision": "skip", "company": f"公司{i}", "job": "A|B\n<script>", "reason": "跳过"}
            for i in range(60)]
    md = jev.render_queue_md(rows, {"stop_reason": "风控退出"})
    assert "公司49" in md and "公司50" not in md
    assert "仅展示前 50 行，共 60 行" in md
    assert "A&#124;B<br>&lt;script&gt;" in md
    assert "停止原因：风控退出" in md
    assert "keyword 对照" not in md


def test_hard_uses_weakest_dimension():
    raw = answers()
    raw["schedule"]["noul"] = 0.1
    result = jev.decide(raw)
    assert result["decision"] == "skip"
    assert "到岗/时长/城市 0.10" in result["reason"]
    assert result["hard_ok"] == 0.1


def test_clean_jd_strips_boss_noise():
    raw = "举报\n微信扫码分享\n职位描述\n关于泡泡星boss球 来自BOSS直聘洗车kanzhun\n任职要求：本科\n 彭女士 刚刚活跃\n 广州影子科技\n工作地址\n深圳"
    assert jev.clean_jd(raw) == "关于泡泡星球 洗车\n任职要求：本科"
