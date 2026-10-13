"""官网入口补全离线回归：所有 HTTP 调用均用替身，禁止真实网络。"""

import copy
import html
import io
import json
import re
import socket
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError

import pytest

import official_apply as official

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone(timedelta(hours=8)))
COMPANY = {"company": "示例科技", "jobs": ["AI 产品实习", "Agent 开发"], "boss_urls": []}
TAVILY_KEY = "test-tavily-secret"
JEV_KEY = "test-jev-secret"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("禁止真实网络请求")
    monkeypatch.setattr(official.request, "urlopen", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


class Response(io.BytesIO):
    def __init__(self, data=b"page", status=200):
        super().__init__(data if isinstance(data, bytes) else json.dumps(data).encode())
        self.status = status
        self.read_sizes = []

    def getcode(self):
        return self.status

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


def answers(p=0.8, choice="0", job_match=0.7):
    return {"answers": {
        "official": {"type": "choice", "choice": choice, "confidence": 0.01,
                     "probabilities": {"0": p, "1": (1 - p) / 2, "2": (1 - p) / 2}},
        "job_match": {"type": "noul", "noul": job_match},
    }}


def candidate(url="https://jobs.example.com/apply", p=0.8, choice="0", job_match=0.7,
              kind="ATS/招聘子站", http=200):
    row = {"url": url, "title": "示例科技招聘", "official": p, "official_choice": choice,
           "job_match": job_match, "kind": kind, "http_status": http,
           "checked_at": NOW.isoformat(), "reason": "待人工核验"}
    row["status"] = official.candidate_status(row)
    return row


def result(candidates=None, company=None):
    company = copy.deepcopy(company or COMPANY)
    rows = official.select_candidates([candidate()] if candidates is None else candidates)
    return {**company, "queries": official.build_queries(company["company"], company["jobs"]),
            "matched_job": company["jobs"][0] if company["jobs"] else "",
            "source_urls": [row["url"] for row in rows], "candidates": rows,
            "status": official.company_status(rows), "errors": [], "cached": False}


@pytest.mark.parametrize("domain", official.AGGREGATORS)
def test_aggregators_and_subdomains_are_blocked(domain):
    assert official.is_aggregator(f"https://{domain}")
    host, _, path = domain.partition("/")
    assert official.is_aggregator(f"https://sub.{host}/{path}")


@pytest.mark.parametrize("url", [
    "https://WWW.ZHIPIN.COM./job_detail/123", "https://zhaopin.baidu.com/jobs",
    "https://www.baidu.com/%7Ahaopin/jobs", "https://foo.zhaopin.baidu.com/",
    "https://new.qq.com/rain/a/123", "https://zhipin.com@www.zhiye.com/a",
    "javascript:alert(1)", "file:///etc/passwd", "/careers", "https://[broken",
    "https://example.com:invalid/jobs", "https://example.com/a\nb", None, 12,
])
def test_invalid_or_aggregator_urls_rejected(url):
    assert official.is_aggregator(url)


@pytest.mark.parametrize("url", [
    "https://hr.163.com/", "https://join.qq.com/", "https://careers.tencent.com/",
    "https://www.baidu.com/about/", "https://zhipin.com.example.org/jobs",
    "https://notzhipin.com/jobs", "https://example.com/?ref=zhipin.com",
])
def test_official_exceptions_and_domain_boundaries(url):
    assert not official.is_aggregator(url)


@pytest.mark.parametrize("domain", official.ATS_DOMAINS)
def test_explicit_ats_domains(domain):
    assert official.classify_kind(f"https://{domain}") == "ATS/招聘子站"


@pytest.mark.parametrize("prefix", ["campus", "career", "careers", "job", "jobs", "hr", "zhaopin", "talent", "join"])
def test_recruit_subdomain_prefix(prefix):
    assert official.classify_kind(f"https://{prefix}.example.com/") == "ATS/招聘子站"


@pytest.mark.parametrize("path", ["career", "careers", "job", "jobs", "zhaopin", "recruit", "campus", "join", "%63areers"])
def test_recruit_path(path):
    assert official.classify_kind(f"https://www.example.com/{path}/open") == "招聘页"


@pytest.mark.parametrize("url", ["https://example.com/", "https://example.com/?next=jobs",
                                     "https://jobs-example.com/", "https://feishu.cn/about/",
                                     "https://mokahr.com.fake.org/", "not a url"])
def test_other_kind(url):
    assert official.classify_kind(url) == "其他"


def test_collect_logs_deduplicates_merges_and_honors_buckets(tmp_path):
    data = {
        "applied": [{"company": " A C M E ", "job": "产品", "url": "https://boss.example/a"},
                    {"company": "\t\n", "job": "空公司"}, {}, None, {"company": 1}],
        "pending": [{"company": "Acme", "job": "研发", "url": "https://boss.example/b"}],
        "review": [{"company": "review公司", "decision": "review"},
                   {"company": "apply-review", "decision": "apply"}],
        "skipped": [{"company": "不应收集", "decision": "apply"}],
        "failed": [{"company": "失败后待投", "decision": "apply"}, {"company": "失败未决"}],
        "dry_run": [{"company": "待投", "decision": "apply"}], "metadata": "ignore",
    }
    (tmp_path / "boss-a-log.json").write_text(json.dumps(data))
    (tmp_path / "boss-b-log.json").write_text(json.dumps({
        "applied": [{"company": "acme", "job": "产品", "url": "https://boss.example/a"}]}))
    (tmp_path / "boss-broken-log.json").write_text("{")
    (tmp_path / "boss-list-log.json").write_text("[]")
    (tmp_path / "boss-bad-bucket-log.json").write_text('{"applied": {}}')
    (tmp_path / "lagou-a-log.json").write_text(json.dumps(data))
    rows = official.collect_companies(tmp_path)
    assert len(rows) == 5
    assert rows[0] == {"company": "A C M E", "jobs": ["产品", "研发"],
                       "boss_urls": ["https://boss.example/a", "https://boss.example/b"]}
    assert {r["company"] for r in rows} == {"A C M E", "review公司", "apply-review", "失败后待投", "待投"}


def test_tavily_key_env_precedence_fallback_and_missing(monkeypatch, tmp_path, capsys):
    path = tmp_path / "search.env"
    path.write_text('# config\nOTHER_KEY=ignore\nTAVILY_API_KEY = "file-only-secret"\n')
    monkeypatch.setenv("TAVILY_API_KEY", "  env-only-secret  ")
    assert official.load_tavily_key(path) == "env-only-secret"
    monkeypatch.setenv("TAVILY_API_KEY", "  ")
    assert official.load_tavily_key(path) == "file-only-secret"
    monkeypatch.delenv("TAVILY_API_KEY")
    assert official.load_tavily_key(tmp_path / "missing") == ""
    path.write_bytes(b"\xff")
    assert official.load_tavily_key(path) == ""
    assert capsys.readouterr() == ("", "")


def test_tavily_default_key_file(monkeypatch, tmp_path):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    path = tmp_path / "search.env"
    path.write_text("TAVILY_API_KEY='file-only-secret'\n")
    monkeypatch.setattr(official.Path, "expanduser", lambda self: path)
    assert official.load_tavily_key() == "file-only-secret"


def test_tavily_request_contract(monkeypatch):
    returned = [{"url": "https://jobs.example.com/", "title": "招聘", "content": "岗位"}]

    def urlopen(req, timeout):
        assert req.full_url == "https://api.tavily.com/search"
        assert req.method == "POST" and timeout == 30
        assert req.get_header("Authorization") == f"Bearer {TAVILY_KEY}"
        assert req.get_header("Content-type") == "application/json"
        assert json.loads(req.data) == {
            "query": "示例科技 校园招聘 官网 实习", "max_results": 8,
            "search_depth": "basic", "include_answer": False,
            "exclude_domains": official.AGGREGATORS,
        }
        assert TAVILY_KEY.encode() not in req.data
        return Response({"results": returned})

    monkeypatch.setattr(official.request, "urlopen", urlopen)
    assert official.tavily_search("示例科技 校园招聘 官网 实习", TAVILY_KEY) == returned


def test_jev_questions_state_and_parsed_probabilities(monkeypatch):
    raw = {"url": "https://jobs.example.com/", "title": "招聘", "content": "摘" * 1600}

    def urlopen(req, timeout):
        assert req.full_url == official.JEV_URL and req.method == "POST" and timeout == 30
        assert req.get_header("Authorization") == f"Bearer {JEV_KEY}"
        body = json.loads(req.data)
        assert set(body) == {"model", "state", "questions"}
        assert body["model"] == official.JEV_MODEL
        assert body["state"] == ("【公司】示例科技\n【Boss岗位】产品实习\n【网页标题】招聘\n"
                                 "【URL】https://jobs.example.com/\n【摘要】" + "摘" * 1500)
        assert body["questions"] == {
            "official": {"type": "choice",
                         "instructions": "该网页是否为『示例科技』官方的招聘/校招页面或其官方使用的招聘系统页面？",
                         "criteria": {"0": "是该公司官方招聘入口", "1": "第三方聚合/新闻/无关页面", "2": "是其他公司的页面"}},
            "job_match": {"type": "noul", "instructions": "该页面是否包含『产品实习』或高度相似的具体职位？"},
        }
        return Response(answers())

    monkeypatch.setattr(official.request, "urlopen", urlopen)
    assert official.call_jev("示例科技", "产品实习", raw, JEV_KEY) == {
        "official_choice": "0", "official": 0.8, "job_match": 0.7}


@pytest.mark.parametrize("change", [
    {"official": {}}, {"job_match": {"noul": True}}, {"job_match": {"noul": float("nan")}},
    {"job_match": {"noul": -1}}, {"job_match": {"noul": 1.1}},
    {"official": {"choice": "0", "probabilities": {"0": 0.8}}},
    {"official": {"choice": "3", "probabilities": {"0": 0.8, "1": 0.1, "2": 0.1}}},
    {"official": {"choice": "0", "probabilities": {"0": 0.8, "1": 0.5, "2": 0.5}}},
    {"official": {"choice": "0", "probabilities": {"0": "0.8", "1": 0.1, "2": 0.1}}},
])
def test_invalid_jev_answers_fail_closed(change):
    raw = answers()["answers"]
    raw.update(change)
    with pytest.raises(official.JevError):
        official.parse_jev_answers(raw)


@pytest.mark.parametrize("service", ["tavily", "jev"])
@pytest.mark.parametrize("failure", ["http", "network", "json", "shape"])
def test_api_failures_do_not_expose_key_or_body(monkeypatch, service, failure):
    def urlopen(*args, **kwargs):
        if failure == "http":
            raise HTTPError("https://example.com/" + TAVILY_KEY, 401, JEV_KEY, {}, io.BytesIO(JEV_KEY.encode()))
        if failure == "network":
            raise URLError(TAVILY_KEY + JEV_KEY)
        return Response((TAVILY_KEY + JEV_KEY).encode() if failure == "json" else {"answers": [], "results": {}})

    monkeypatch.setattr(official.request, "urlopen", urlopen)
    with pytest.raises((official.SearchError, official.JevError)) as exc:
        if service == "tavily":
            official.tavily_search("query", TAVILY_KEY)
        else:
            official.call_jev("公司", "岗位", {"url": "https://example.com", "title": "", "content": ""}, JEV_KEY)
    assert TAVILY_KEY not in str(exc.value) and JEV_KEY not in str(exc.value)
    if failure == "http":
        assert "401" in str(exc.value)


@pytest.mark.parametrize("p,choice,http,status", [
    (0.399999, "0", 200, "未找到"), (0.4, "0", None, "待核验"),
    (0.599999, "0", 200, "待核验"), (0.6, "0", 200, "已核验"),
    (1, "0", 299, "已核验"), (0.6, "0", 301, "已核验"), (0.6, "0", 399, "已核验"),
    (0.6, "0", 199, "待核验"), (0.6, "0", 400, "待核验"), (0.9, "0", 403, "待核验"),
    (0.9, "0", None, "待核验"), (0.7, "1", 200, "未找到"), (0.9, "2", 200, "未找到"),
])
def test_candidate_status_boundaries(p, choice, http, status):
    row = candidate(p=p, choice=choice, http=http)
    assert official.candidate_status(row) == status
    selected = official.select_candidates([row])
    assert official.company_status(selected) == status
    assert bool(selected) == (status != "未找到")


def test_sort_probability_then_kind_then_job_match_and_deduplicate():
    rows = [candidate("https://example.com/low", p=0.7),
            candidate("https://example.com/other", p=0.8, kind="其他", job_match=1),
            candidate("https://example.com/jobs", p=0.8, kind="招聘页", job_match=1),
            candidate("https://jobs.example.com/a", p=0.8, job_match=0.6),
            candidate("https://jobs.example.com/b", p=0.8, job_match=0.9),
            candidate("https://example.com/high", p=0.9, kind="其他", job_match=0),
            candidate("https://example.com/rejected", p=0.99, choice="2")]
    before = copy.deepcopy(rows)
    selected = official.select_candidates(rows + [rows[-2]])
    assert [r["url"] for r in selected] == [rows[-2]["url"], rows[4]["url"]]
    assert official.select_candidates(rows[1:5])[1]["url"] == rows[3]["url"]
    assert official.select_candidates(rows[1:3])[0]["kind"] == "招聘页"
    assert rows == before
    assert official.company_status([]) == "未找到"
    assert official.company_status([candidate(http=None), candidate()]) == "已核验"


@pytest.mark.parametrize("status", [200, 204, 301, 399, 403, 500])
def test_reachability_get_timeout_and_read_limit(monkeypatch, status):
    response = Response(b"x" * (official.MAX_PAGE_BYTES + 20), status)

    def urlopen(req, timeout):
        assert req.full_url == "https://example.com/jobs" and req.method == "GET"
        assert req.data is None and timeout == 8
        assert req.get_header("User-agent").startswith("Mozilla/5.0")
        assert req.get_header("Authorization") is None
        return response

    monkeypatch.setattr(official.request, "urlopen", urlopen)
    code, reason = official.check_reachable("https://example.com/jobs")
    assert code == status and bool(reason) == (status >= 400)
    assert response.read_sizes == [200 * 1024] and response.closed


@pytest.mark.parametrize("failure", [HTTPError("https://example.com", 403, "secret", {}, None),
                                     HTTPError("https://example.com", 302, "secret", {}, None),
                                     TimeoutError("secret"), URLError("secret")])
def test_reachability_error_is_sanitized(monkeypatch, failure):
    def urlopen(*args, **kwargs):
        raise failure
    monkeypatch.setattr(official.request, "urlopen", urlopen)
    status, reason = official.check_reachable("https://example.com")
    assert status == getattr(failure, "code", None) and "secret" not in reason
    assert bool(reason) == (status != 302)


def test_find_official_two_queries_six_candidates_and_no_invented_urls(monkeypatch):
    calls, gets, states = [], [], []
    urls = [f"https://jobs.example.com/{i}?x=1&y=2#position" for i in range(8)]
    raw = [{"url": "https://www.zhipin.com/job_detail/a", "title": "聚合"},
           {"url": "https://jobs.example.com/secret?key=" + TAVILY_KEY}, None,
           *[{"url": url, "title": "招聘", "content": "摘要"} for url in urls]]
    raw[3]["title"] = "招聘 " + TAVILY_KEY

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if req.full_url == official.TAVILY_URL:
            return Response({"results": raw})
        if req.full_url == official.JEV_URL:
            state = json.loads(req.data)["state"]
            states.append(state)
            index = next(i for i, url in enumerate(urls) if url in state)
            if index == 1:
                return Response(answers(0.599999))
            if index == 2:
                return Response(answers(0.8, choice="2"))
            if index == 3:
                return Response(answers(0.399999))
            return Response(answers(0.9 if index == 4 else 0.6))
        gets.append(req.full_url)
        return Response(status=403 if req.full_url == urls[4] else 200)

    monkeypatch.setattr(official.request, "urlopen", urlopen)
    actual = official.find_official(COMPANY, TAVILY_KEY, JEV_KEY, NOW)
    assert actual["queries"] == ["示例科技 校园招聘 官网 实习", "示例科技 AI 产品实习 招聘"]
    assert calls.count(official.TAVILY_URL) == 2 and calls.count(official.JEV_URL) == 6
    assert gets == [urls[0], urls[4], urls[5]]
    assert [c["url"] for c in actual["candidates"]] == [urls[4], urls[0]]
    assert [c["status"] for c in actual["candidates"]] == ["待核验", "已核验"]
    assert actual["status"] == "已核验" and not actual["errors"]
    assert TAVILY_KEY not in json.dumps(actual) + "".join(states)
    assert actual["source_urls"] == urls[:6] and all(url in urls for url in gets)


@pytest.mark.parametrize("service", ["tavily", "jev"])
def test_find_official_survives_service_errors(monkeypatch, service):
    def urlopen(req, timeout):
        if req.full_url == official.TAVILY_URL and service != "tavily":
            return Response({"results": [{"url": "https://example.com/jobs", "title": "", "content": ""}]})
        raise URLError("secret")
    monkeypatch.setattr(official.request, "urlopen", urlopen)
    actual = official.find_official(COMPANY, TAVILY_KEY, JEV_KEY, NOW)
    assert actual["status"] == "未找到" and actual["errors"]
    assert "secret" not in json.dumps(actual)


@pytest.mark.parametrize("age,hit", [(timedelta(0), True), (timedelta(days=29), True),
                                     (timedelta(days=30, microseconds=-1), True),
                                     (timedelta(days=30), False), (timedelta(days=31), False),
                                     (timedelta(seconds=-1), False)])
def test_cache_ttl_and_normalized_company(age, hit):
    company = {"company": " A c m e ", "jobs": ["产品"], "boss_urls": []}
    cache = {"acme": {"ts": (NOW - age).isoformat(), "result": result(company=company)}}
    before = copy.deepcopy(cache)
    actual = official.get_cached(cache, {**company, "company": "ACME"}, NOW)
    assert (actual is not None) == hit
    if hit:
        assert actual["cached"] and actual["company"] == "ACME"
        assert actual["candidates"][0]["checked_at"] == NOW.isoformat()
    assert cache == before


def test_cache_changed_first_job_does_not_reuse_old_job_match():
    cache = {"示例科技": {"ts": NOW.isoformat(), "result": result()}}
    actual = official.get_cached(cache, {**COMPANY, "jobs": ["视觉设计", "产品"]}, NOW)
    assert actual["jobs"] == ["视觉设计", "产品"]
    assert actual["candidates"][0]["job_match"] is None
    assert actual["queries"] == cache["示例科技"]["result"]["queries"]
    assert cache["示例科技"]["result"]["candidates"][0]["job_match"] == 0.7


def test_cache_files_roundtrip_and_broken_cache(tmp_path):
    path = tmp_path / "official-cache.json"
    assert official.load_cache(path) == {}
    for content in ("{", "[]", "null"):
        path.write_text(content)
        assert official.load_cache(path) == {}
    cache = {"示例科技": {"ts": NOW.isoformat(), "result": result([])}}
    official.save_cache(path, cache)
    assert official.load_cache(path) == cache
    assert official.get_cached(cache, COMPANY, NOW)["status"] == "未找到"


@pytest.mark.parametrize("damage", ["ts", "result", "url", "probability", "errors", "company"])
def test_corrupt_cache_misses(damage):
    entry = {"ts": NOW.isoformat(), "result": result()}
    if damage == "ts":
        entry["ts"] = "broken"
    elif damage == "result":
        entry["result"] = []
    elif damage == "url":
        entry["result"]["candidates"][0]["url"] = "https://invented.example.com/"
    elif damage == "probability":
        entry["result"]["candidates"][0]["official"] = float("nan")
    elif damage == "company":
        entry["result"]["company"] = "其他公司"
    else:
        entry["result"]["errors"] = ["查询失败"]
    assert official.get_cached({"示例科技": entry}, COMPANY, NOW) is None


@pytest.mark.parametrize("value,label", [(0, "否"), (0.4, "否"), (0.400001, "不确定"),
                                        (0.599999, "不确定"), (0.6, "是"), (1, "是"), (None, "不确定")])
def test_job_match_boundaries(value, label):
    assert official.job_match_label(value) == label


def test_render_pure_tables_links_counts_and_escaping():
    verified = result([candidate("https://jobs.example.com/view(1)?a=1&b=2", job_match=0.6)])
    verified["candidates"][0]["title"] = "这是一个超过三十个汉字的官网招聘页面标题用来测试链接标题会被正确截断并保持可读"
    pending = result([candidate("https://example.com/jobs", p=0.4, http=None, job_match=0.4)])
    pending["company"] = "待核验公司 | [伪链接](https://evil.example/)"
    pending["candidates"][0]["title"] = "[伪链接](https://evil.example/)"
    missing = result([])
    missing["company"] = "未找到公司"
    data = [verified, pending, missing]
    before = copy.deepcopy(data)
    rendered = official.render_official_md(data, NOW)
    assert data == before
    for heading in ("## 已核验", "## 待核验", "## 未找到"):
        assert heading in rendered
    assert "已核验：1｜待核验：1｜未找到：1" in rendered and "查了 3 家" in rendered
    assert "AI 产品实习、Agent 开发" in rendered and "| 原因 |" in rendered
    assert "链接均来自搜索结果，投递前请人工确认岗位仍开放" in rendered
    assert verified["candidates"][0]["title"][:30] in rendered
    assert verified["candidates"][0]["title"] not in rendered
    assert "| 是 | 200 |" in rendered and "| 否 | — |" in rendered
    assert "未找到公司 — 已尝试的查询：示例科技 校园招聘 官网 实习" in rendered
    links = [html.unescape(url) for url in re.findall(r"\]\(<([^>]+)>\)", rendered)]
    assert links == [verified["candidates"][0]["url"], pending["candidates"][0]["url"]]
    assert "[伪链接](" not in rendered and NOW.isoformat() in rendered


def test_render_empty_and_partial_failures():
    assert "查了 0 家" in official.render_official_md([], NOW)
    failed = result([])
    failed["errors"] = ["Tavily HTTP 错误 401"]
    rendered = official.render_official_md([failed], NOW)
    assert "查询未完成" in rendered and "## 查询异常" in rendered and "401" in rendered


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setattr(official, "__file__", str(tmp_path / "official_apply.py"))
    monkeypatch.setattr(official, "load_tavily_key", lambda: TAVILY_KEY)
    monkeypatch.setattr(official, "load_jev_key", lambda: JEV_KEY)
    return tmp_path


def test_cli_end_to_end_cache_and_refresh_mocking_only_http(cli, monkeypatch, capsys):
    calls = []

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if req.full_url == official.TAVILY_URL:
            return Response({"results": [{"url": "https://jobs.example.com/role?x=1&y=2",
                                          "title": "招聘 " + TAVILY_KEY, "content": "职位"}]})
        if req.full_url == official.JEV_URL:
            return Response(answers())
        return Response()

    monkeypatch.setattr(official.request, "urlopen", urlopen)
    args = ["--company", "示例科技", "--job", "产品实习"]
    assert official.main(args) == 0
    assert len(calls) == 4  # 两次搜索、一次 Jev、一次只读 GET。
    cache_text = (cli / "official-cache.json").read_text()
    cache = json.loads(cache_text)
    assert cache["示例科技"]["result"]["candidates"][0]["status"] == "已核验"
    cache_ts = cache["示例科技"]["ts"]
    assert official.main(args) == 0 and len(calls) == 4
    assert "缓存 1 家" in (cli / "官网投递.md").read_text()
    assert json.loads((cli / "official-cache.json").read_text())["示例科技"]["ts"] == cache_ts
    assert official.main([*args, "--refresh"]) == 0 and len(calls) == 8
    assert "缓存 0 家" in (cli / "官网投递.md").read_text()
    stdout, stderr = capsys.readouterr()
    saved = cache_text + (cli / "官网投递.md").read_text() + stdout + stderr
    assert TAVILY_KEY not in saved and JEV_KEY not in saved
    assert "https://jobs.example.com/role?x=1&amp;y=2" in saved


def test_cli_limit_cached_companies_do_not_starve_later_rows(cli, monkeypatch):
    (cli / "boss-test-log.json").write_text(json.dumps({"applied": [
        {"company": f"公司{i}", "job": "产品"} for i in range(3)]}))
    searches = []

    def find(company, *args):
        searches.append(company["company"])
        return result([], company)

    monkeypatch.setattr(official, "find_official", find)
    assert official.main(["--limit", "1"]) == 0 and searches == ["公司0"]
    assert official.main(["--limit", "1"]) == 0 and searches == ["公司0", "公司1"]
    assert "查了 2 家" in (cli / "官网投递.md").read_text()
    assert official.main(["--limit", "1", "--refresh"]) == 0
    assert searches == ["公司0", "公司1", "公司0"]


def test_cli_failures_not_cached_and_previous_report_replaced(cli, monkeypatch, capsys):
    (cli / "官网投递.md").write_text("过时内容")

    def urlopen(*args, **kwargs):
        raise URLError(TAVILY_KEY)

    monkeypatch.setattr(official.request, "urlopen", urlopen)
    assert official.main(["--company", "示例科技", "--job", "产品"]) == 1
    report = (cli / "官网投递.md").read_text()
    assert "过时内容" not in report and "查询异常" in report
    assert json.loads((cli / "official-cache.json").read_text()) == {}
    stdout, stderr = capsys.readouterr()
    assert TAVILY_KEY not in stdout + stderr + report


@pytest.mark.parametrize("missing", ["tavily", "jev"])
def test_cli_missing_key_exits_without_writing(cli, monkeypatch, missing, capsys):
    monkeypatch.setattr(official, f"load_{missing}_key", lambda: "")
    assert official.main(["--company", "示例科技"]) == 1
    assert "缺少" in capsys.readouterr().err
    assert not (cli / "官网投递.md").exists() and not (cli / "official-cache.json").exists()


@pytest.mark.parametrize("args", [["--limit", "0"], ["--limit", "-1"], ["--job", "产品"], ["--company", " "]])
def test_cli_bad_arguments(args):
    with pytest.raises(SystemExit) as exc:
        official.main(args)
    assert exc.value.code == 2


def test_help_does_not_read_credentials_or_call_network(monkeypatch, capsys):
    monkeypatch.setattr(official, "load_tavily_key", lambda: pytest.fail("help 不应读取凭证"))
    with pytest.raises(SystemExit) as exc:
        official.main(["--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert all(flag in text for flag in ("--company", "--job", "--limit", "--refresh"))
