"""Boss P1 离线流程回归：浏览器、沟通与 Jev 均替身。"""

import json
import re
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import boss_apply as boss
import jev_match as jev
from shared import DEFAULT_CONFIG, load_applied_index, load_log
from test_jev_match import answers


class FakePage:
    def __init__(self, cards):
        self.cards = cards
        self.current = None
        self.url = "https://www.zhipin.com/web/geek/job"
        self.opened = []
        self.scrolled = 0
        self.blocked = ""
        self.navigations = []
        self.new_tabs = []

    def get_tabs(self):
        return [self]

    def get(self, url):
        self.navigations.append(url)
        self.url = url

    def new_tab(self, url):
        self.new_tabs.append(url)
        self.url = url
        return self

    def run_js(self, js, *args):
        if js == boss.CARD_SNAPSHOT_JS:
            return self.cards
        if js.startswith('var b=document.querySelector(".op-btn-chat")'):
            return self.current.get("disabled", False)
        if js.startswith("window.scrollTo"):
            self.scrolled += 1
            return None
        pytest.fail(f"未知浏览器操作: {js[:50]}")

    def ele(self, selector, timeout=None):
        if selector.startswith("css:.job-card-wrap"):
            job_id = re.search(r"job_detail/([^/]+)\.html", selector)[1]
            self.current = next(card for card in self.cards if card["job_id"] == job_id)
            return self
        if selector == "css:.job-card-box":
            return self
        if selector in (".job-detail-body", ".job-sec-text"):
            if self.current.get("missing_jd"):
                return None
            return SimpleNamespace(text=self.current.get("jd", "AI 产品实习 Agent"))
        pytest.fail(f"未知选择器: {selector}")

    def parent(self, selector):
        return self

    def click(self):
        self.opened.append(self.current["job_id"])
        if self.current.get("block_on_open"):
            self.blocked = "详情触发验证"
            self.url = "https://www.zhipin.com/web/passport/zp/verify.html"
        if self.current.get("click_error"):
            raise RuntimeError("点击结果未知")

    def eles(self, selector):
        return self.cards


def card(job_id, **kwargs):
    return {"job_id": job_id, "title": "AI产品经理实习", "company": f"公司{job_id}", "salary": "200/天", **kwargs}


@pytest.fixture
def harness(monkeypatch, tmp_path):
    def setup(cards, responses=None, chat_statuses=None, title_responses=None, **overrides):
        cfg = {**DEFAULT_CONFIG, "resume_path": "resume.txt", "job_profile": "张三，2027届，非985/211，深圳",
               "resume_redact": ["张三"], "target_roles": ["产品经理"], "exclude_keywords": ["资深"], **overrides}
        (tmp_path / "resume.txt").write_text("张三 13812345678 test@example.com\nAgent 产品项目")
        page = FakePage(cards)
        calls, chats, sleeps = [], [], []
        title_calls = []
        response_iter = iter(responses) if responses is not None else None
        chat_iter = iter(chat_statuses) if chat_statuses is not None else None
        title_iter = iter(title_responses) if title_responses is not None else None

        def fake_title_call(state, key, timeout=30):
            title_calls.append(state)
            assert key == "offline-only-key"
            assert all(private not in state for private in ("张三", "13812345678", "test@example.com"))
            result = next(title_iter) if title_iter else {"choice": "0", "probabilities": {"0": 1.0}}
            if isinstance(result, Exception):
                raise result
            return result

        def fake_call(state, key, timeout=30):
            calls.append(state)
            assert key == "offline-only-key"
            assert all(private not in state for private in ("张三", "13812345678", "test@example.com"))
            result = next(response_iter) if response_iter else answers()
            if isinstance(result, Exception):
                raise result
            return result

        def fake_chat(page, job_id):
            chats.append(job_id)
            status = next(chat_iter) if chat_iter else "chat_started"
            return status, f"测试 {status}", {"packets": []}, True

        monkeypatch.setattr(boss, "__file__", str(tmp_path / "boss_apply.py"))
        monkeypatch.setattr(boss, "load_config", lambda: cfg)
        monkeypatch.setattr(boss, "load_applied_index", lambda: load_applied_index(tmp_path))
        monkeypatch.setattr(boss, "load_jev_key", lambda: "offline-only-key")
        monkeypatch.setattr(boss, "connect_chrome", lambda: page)
        monkeypatch.setattr(boss, "browse_sleep", lambda *a: None)
        monkeypatch.setattr(boss.time, "sleep", sleeps.append)
        monkeypatch.setattr(boss.random, "uniform", lambda low, high: low)
        monkeypatch.setattr(boss, "page_block_reason", lambda page: page.blocked)
        monkeypatch.setattr(boss, "wait_detail_title", lambda *a: not page.current.get("title_mismatch"))
        monkeypatch.setattr(boss, "start_chat", fake_chat)
        monkeypatch.setattr(jev, "call_jev", fake_call)
        monkeypatch.setattr(jev, "call_title_jev", fake_title_call)
        monkeypatch.setattr(jev.request, "urlopen", lambda *a, **k: pytest.fail("禁止联网"))

        def run(*flags):
            monkeypatch.setattr(sys, "argv", ["boss_apply.py", "--job", "产品经理", "--city", "深圳", *flags])
            boss.main()

        return SimpleNamespace(run=run, calls=calls, chats=chats, page=page, sleeps=sleeps,
                               title_calls=title_calls,
                               log=lambda: load_log(tmp_path / "boss-深圳-log.json"),
                               queue=lambda: (tmp_path / "boss-深圳-队列.md").read_text())
    return setup


def test_jev_dry_run_decisions_and_keyword_comparison(harness):
    h = harness([card("a", jd="实习生有资深导师带教"), card("b"), card("c")],
                responses=[answers(), answers(hard=0.45), answers(hard=0.01)])
    h.run("--dry-run")
    assert len(h.calls) == 3  # 正文“资深”不拦截
    assert not h.chats
    log = h.log()
    assert not log["applied"] and not log["pending"]
    assert len(log["review"]) == len(log["skipped"]) == 1
    assert log["review"][0]["url"] == "https://www.zhipin.com/job_detail/b.html"
    assert "hard=0.01" in log["skipped"][0]["reason"]
    assert "推荐：1｜复核：1｜跳过：1｜已沟通：0｜待核验：0" in h.queue()
    assert "| 公司a | AI产品经理实习 | apply | 0 |" in h.queue()


def test_pre_filters_run_before_jev(harness, tmp_path):
    (tmp_path / "boss-old-log.json").write_text(json.dumps({"pending": [{"job_id": "old"}]}))
    h = harness([card(""), card("black", company="外包服务"), card("old"),
                 card("title", title="资深产品经理"), card("ok", jd="资深导师带教")],
                company_blacklist=["外包"])
    h.run("--dry-run")
    assert h.page.opened == ["ok"]
    assert len(h.calls) == 1
    assert len(h.log()["skipped"]) == 4


def test_review_can_be_revisited_from_cache(harness, tmp_path):
    h = harness([card("review")], responses=[answers(hard=0.45)])
    h.run()
    h.run()
    assert len(h.calls) == 1
    assert len(h.log()["review"]) == 2
    assert not h.chats
    assert load_applied_index(tmp_path) == set()


def test_count_limit_queues_rest_of_loaded_batch(harness):
    h = harness([card("a"), card("b"), card("c")])
    h.run("--count", "1", "--fill-queue")
    assert h.chats == ["a"]
    assert len(h.calls) == 3
    assert h.page.scrolled == 0
    assert h.sleeps == [6, 30, 6, 6]
    assert len(h.log()["applied"]) == 1
    queue = h.queue()
    assert "推荐：3｜复核：0｜跳过：0｜已沟通：1｜待核验：0" in queue
    assert "公司b" in queue and "公司c" in queue
    assert "已达 count 上限，未沟通" in queue
    assert "| 公司a |" not in queue


def test_dry_run_count_still_limits_evaluations(harness):
    h = harness([card("a"), card("b")])
    h.run("--dry-run", "--count", "1")
    assert len(h.calls) == 1
    assert h.page.opened == ["a"]
    assert not h.chats


def test_pending_does_not_count_and_p0_dedup_remains(harness, tmp_path):
    h = harness([card("a"), card("b")], chat_statuses=["pending", "chat_started"])
    h.run("--count", "1", "--fast")
    assert h.chats == ["a", "b"]
    assert h.sleeps == [1, 3, 1]
    assert len(h.log()["pending"]) == len(h.log()["applied"]) == 1
    assert {"boss:a", "boss:b"} <= load_applied_index(tmp_path)
    assert "推荐：2｜复核：0｜跳过：0｜已沟通：1｜待核验：1" in h.queue()
    assert "| 公司a |" not in h.queue()
    h.run()
    assert len(h.calls) == 2 and h.chats == ["a", "b"]


def test_p0_extra_rest_every_five_successes(harness):
    h = harness([card(str(i)) for i in range(5)])
    h.run("--count", "6")
    assert h.sleeps == [6, 30, 6, 30, 6, 30, 6, 30, 6, 30, 180]


def test_jev_failures_review_and_stop_at_three(harness):
    h = harness([card(str(i)) for i in range(4)], responses=[jev.JevError()] * 4)
    with pytest.raises(SystemExit) as exc:
        h.run()
    assert exc.value.code == 1
    assert len(h.calls) == 3
    assert not h.chats
    assert len(h.log()["review"]) == 3
    assert all(r["reason"] == "Jev 调用失败" for r in h.log()["review"])
    assert "停止原因：Jev 连续调用失败 3 次" in h.queue()


def test_success_resets_failure_streak(harness):
    h = harness([card(str(i)) for i in range(6)],
                responses=[jev.JevError(), jev.JevError(), answers(hard=0.45),
                           jev.JevError(), jev.JevError(), answers(hard=0.45)])
    h.run()
    assert len(h.calls) == 6 and len(h.log()["review"]) == 6
    assert not h.chats


@pytest.mark.parametrize("where", ["page", "detail", "chat"])
def test_blocked_exit_always_writes_queue(harness, tmp_path, where):
    h = harness([card("a", block_on_open=where == "detail"), card("b")], chat_statuses=["blocked"])
    if where == "page":
        h.page.blocked = "页面风控"
    with pytest.raises(SystemExit) as exc:
        h.run()
    assert exc.value.code == 1
    assert len(h.log()["failed"]) == 1
    assert h.log()["failed"][0]["status"] == "blocked"
    assert "停止原因" in h.queue()
    assert len(h.chats) == (1 if where == "chat" else 0)
    assert h.sleeps == ([6] if where == "chat" else [])  # 风控后不休息、不加载下一岗位
    cooldown = json.loads((tmp_path / "boss-cooldown.json").read_text())
    remaining = datetime.fromisoformat(cooldown["until"]) - datetime.now().astimezone()
    assert timedelta(hours=1, minutes=59) < remaining <= timedelta(hours=2)
    assert cooldown["reason"] == h.log()["failed"][0]["reason"]
    assert cooldown["page_url"] == h.page.url


def test_missing_key_exits_before_browser(harness, monkeypatch, capsys):
    h = harness([card("a")])
    monkeypatch.setattr(boss, "load_jev_key", lambda: "")
    monkeypatch.setattr(boss, "connect_chrome", lambda: pytest.fail("缺 key 不应启动浏览器"))
    with pytest.raises(SystemExit) as exc:
        h.run()
    assert exc.value.code == 2
    assert "不会降级" in capsys.readouterr().out
    assert "缺少 JEV_API_KEY" in h.queue()
    assert not h.calls and not h.chats


@pytest.mark.parametrize("dry_run", [True, False])
def test_keyword_preserves_score_filter_and_dry_run_logs(harness, monkeypatch, dry_run):
    h = harness([card("a", jd="资深导师带教"), card("b", jd="产品实习"), card("c")])
    monkeypatch.setattr(boss, "load_jev_key", lambda: pytest.fail("keyword 不读 key"))
    h.run("--matcher", "keyword", "--count", "2" if dry_run else "1", *( ["--dry-run"] if dry_run else []))
    assert not h.calls
    assert h.chats == ([] if dry_run else ["b"])
    assert len(h.log()["skipped"]) == (0 if dry_run else 1)
    assert len(h.log()["applied"]) == (0 if dry_run else 1)
    assert h.page.opened == ["a", "b"]
    assert "跳过：1" in h.queue()


def test_build_search_url_filters():
    import boss_apply
    url = boss_apply.build_search_url("AI产品经理", "101280600", "实习", "在校生,应届生", "本科")
    assert url.startswith("https://www.zhipin.com/web/geek/jobs?city=101280600")
    assert "jobType=1902" in url and "experience=108,102" in url and "degree=203" in url
    assert url.endswith("query=AI%E4%BA%A7%E5%93%81%E7%BB%8F%E7%90%86")
    assert "jobType" not in boss_apply.build_search_url("x", "1", "不限")
    with pytest.raises(SystemExit):
        boss_apply.build_search_url("x", "1", "实习", "在校")


@pytest.mark.parametrize("urls,index", [
    ([], None), (["https://example.com", "https://www.zhipin.com/web/chat"], None),
    (["https://example.com", "https://www.zhipin.com/web/geek/jobs?query=AI",
      "https://www.zhipin.com/web/geek/job"], 1),
    (["https://www.zhipin.com/web/geek/job", "https://www.zhipin.com/web/geek/jobs"], 0),
])
def test_pick_boss_tab(urls, index):
    tabs = [SimpleNamespace(url=url) for url in urls]
    assert boss.pick_boss_tab(tabs) is (None if index is None else tabs[index])
    assert [tab.url for tab in tabs] == urls


@pytest.mark.parametrize("reuse", [True, False])
def test_browser_tab_reuse_and_new_tab_fallback(harness, reuse):
    h = harness([card("a")])
    if not reuse:
        h.page.url = "https://example.com"
    h.run("--dry-run")
    assert len(h.page.navigations) == int(reuse)
    assert len(h.page.new_tabs) == int(not reuse)
    assert "zhipin.com/web/geek/jobs?" in h.page.url


def test_title_screening_state_cache_and_no_detail(harness):
    h = harness([card("a", title="销售实习", tags=["销售", "客户拓展"])],
                title_responses=[{"choice": "1", "probabilities": {"1": 0.9, "0": 0.1}}])
    h.run("--dry-run")
    h.run("--dry-run")
    assert not h.page.opened and not h.calls and not h.chats and not h.sleeps
    assert h.title_calls == ["【岗位卡片】销售实习｜公司a｜200/天｜标签: ['销售', '客户拓展']\n"
                             "【求职偏好】[已隐去]，2027届，非985/211，深圳"]
    assert len(h.log()["skipped"]) == 2
    assert h.log()["skipped"][0]["reason"] == "标题粗筛无关 p=0.90"
    assert "跳过：1" in h.queue()


def test_title_failure_allows_details_and_does_not_stop_jd_matching(harness):
    h = harness([card(str(i)) for i in range(4)], title_responses=[jev.JevError()] * 4)
    h.run("--dry-run")
    assert len(h.page.opened) == len(h.title_calls) == len(h.calls) == 4
    assert "推荐：4" in h.queue()


@pytest.mark.parametrize("hard,bucket", [(0.01, "skipped"), (0.45, "review")])
@pytest.mark.parametrize("dry_run", [False, True])
def test_latest_skip_review_does_not_reopen(harness, hard, bucket, dry_run):
    h = harness([card("a")], responses=[answers(hard=hard)])
    flags = ["--dry-run"] if dry_run else []
    h.run(*flags)
    h.run(*flags)
    assert h.page.opened == ["a"]
    assert len(h.calls) == len(h.title_calls) == 1
    assert not h.chats
    assert len(h.log()[bucket]) == 2
    assert h.log()[bucket][-1]["reason"].startswith("[缓存]")
    assert "[缓存]" in h.queue() and "公司a" in h.queue()


def test_expired_latest_reopens_and_refresh_forces_jev(harness, tmp_path):
    h = harness([card("a")], responses=[answers(hard=0.45)] * 2)
    h.run("--dry-run")
    path = tmp_path / "jev-cache.json"
    cache = json.loads(path.read_text())
    cache["latest:a"]["ts"] = (datetime.now().astimezone() - timedelta(days=8)).isoformat()
    path.write_text(json.dumps(cache))
    h.run("--dry-run")
    assert h.page.opened == ["a", "a"] and len(h.calls) == 1  # JD 相同复用 answers
    h.run("--dry-run", "--refresh")
    assert h.page.opened == ["a"] * 3
    assert len(h.calls) == len(h.title_calls) == 2


def test_latest_apply_reopens_and_changed_jd_is_rescored(harness):
    h = harness([card("a")], responses=[answers(), answers(hard=0.01)])
    h.run("--dry-run")
    h.run("--dry-run")
    assert h.page.opened == ["a", "a"] and len(h.calls) == 1
    h.page.cards[0]["jd"] = "改变后的岗位职责"
    h.run("--dry-run")
    assert h.page.opened == ["a"] * 3 and len(h.calls) == 2
    assert h.log()["skipped"][0]["decision"] == "skip"


def test_count_limit_stops_immediately_by_default(harness):
    h = harness([card("a"), card("b"), card("c")])
    h.run("--count", "1")
    assert h.page.opened == h.chats == ["a"]
    assert len(h.calls) == len(h.title_calls) == 1
    assert h.sleeps == [6]  # 投满后直接汇总，不再沟通后休息或扫描
    assert h.page.scrolled == 0
    assert "已达 --count 上限（1）" in h.queue()


@pytest.mark.parametrize("fast", [False, True])
def test_view_interval_extra_rest_and_cap(harness, fast, capsys):
    h = harness([card(str(i)) for i in range(12)])
    h.run("--dry-run", "--max-views", "10", *(["--fast"] if fast else []))
    assert len(h.page.opened) == len(h.calls) == len(h.title_calls) == 10
    assert h.sleeps == ([1] * 10 + [3] if fast else [6] * 10 + [45])
    assert h.page.scrolled == 0
    assert "已达 --max-views 上限（10）" in h.queue()
    assert "已达 --max-views 上限（10）" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["missing_jd", "title_mismatch", "click_error"])
def test_detail_failures_still_rest_and_count_toward_cap(harness, failure):
    h = harness([card("a", **{failure: True}), card("b")])
    h.run("--max-views", "1")
    assert h.page.opened == ["a"] and not h.calls and not h.chats
    assert h.sleeps == [6]
    assert h.page.scrolled == 0
    assert "已达 --max-views 上限（1）" in h.queue()


@pytest.mark.parametrize("flags", [[], ["--fill-queue", "--count", "1"], ["--matcher", "keyword"]])
def test_view_cap_applies_to_real_and_fill_queue(harness, flags):
    h = harness([card("a"), card("b"), card("c")])
    h.run("--max-views", "2", *flags)
    assert h.page.opened == ["a", "b"]
    assert h.page.scrolled == 0
    assert "已达 --max-views 上限（2）" in h.queue()


def test_cooldown_stops_before_browser_and_preserves_queue(harness, tmp_path, monkeypatch, capsys):
    h = harness([card("a")])
    (tmp_path / "boss-cooldown.json").write_text(json.dumps({
        "until": (datetime.now().astimezone() + timedelta(hours=2)).isoformat(),
        "reason": "页面验证", "page_url": "https://www.zhipin.com/web/passport/zp/verify.html",
    }))
    (tmp_path / "boss-深圳-队列.md").write_text("上次队列")
    monkeypatch.setattr(boss, "connect_chrome", lambda: pytest.fail("冷却中不得连接浏览器"))
    with pytest.raises(SystemExit) as exc:
        h.run("--dry-run")
    assert exc.value.code == 2
    assert "剩余约" in capsys.readouterr().out
    assert h.queue() == "上次队列"
    assert not h.title_calls and not h.calls


def test_cooldown_write_failure_still_stops_on_block(harness, tmp_path, capsys):
    h = harness([card("a", block_on_open=True), card("b")])
    (tmp_path / "boss-cooldown.json").mkdir()  # 模拟无法写入冷却文件
    with pytest.raises(SystemExit) as exc:
        h.run()
    assert exc.value.code == 1
    assert h.page.opened == ["a"]
    assert not h.sleeps and not h.chats
    assert len(h.log()["failed"]) == 1
    assert "冷却文件写入失败" in capsys.readouterr().out


@pytest.mark.parametrize("ignore", [False, True])
def test_expired_or_ignored_cooldown_allows_browser(harness, tmp_path, ignore):
    h = harness([card("a")])
    (tmp_path / "boss-cooldown.json").write_text(json.dumps({
        "until": (datetime.now().astimezone() + timedelta(hours=2 if ignore else -2)).isoformat(),
        "reason": "页面验证",
    }))
    h.run("--dry-run", *(["--ignore-cooldown"] if ignore else []))
    assert h.page.opened == ["a"]
