"""Boss P1 离线流程回归：浏览器、沟通与 Jev 均替身。"""

import json
import re
import sys
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

    def new_tab(self, url):
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
            return SimpleNamespace(text=self.current.get("jd", "AI 产品实习 Agent"))
        pytest.fail(f"未知选择器: {selector}")

    def parent(self, selector):
        return self

    def click(self):
        self.opened.append(self.current["job_id"])

    def eles(self, selector):
        return self.cards


def card(job_id, **kwargs):
    return {"job_id": job_id, "title": "AI产品经理实习", "company": f"公司{job_id}", "salary": "200/天", **kwargs}


@pytest.fixture
def harness(monkeypatch, tmp_path):
    def setup(cards, responses=None, chat_statuses=None, **overrides):
        cfg = {**DEFAULT_CONFIG, "resume_path": "resume.txt", "job_profile": "张三，2027届，非985/211，深圳",
               "resume_redact": ["张三"], "target_roles": ["产品经理"], "exclude_keywords": ["资深"], **overrides}
        (tmp_path / "resume.txt").write_text("张三 13812345678 test@example.com\nAgent 产品项目")
        page = FakePage(cards)
        calls, chats, sleeps = [], [], []
        response_iter = iter(responses) if responses is not None else None
        chat_iter = iter(chat_statuses) if chat_statuses is not None else None

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
        monkeypatch.setattr(boss, "wait_detail_title", lambda *a: True)
        monkeypatch.setattr(boss, "start_chat", fake_chat)
        monkeypatch.setattr(jev, "call_jev", fake_call)
        monkeypatch.setattr(jev.request, "urlopen", lambda *a, **k: pytest.fail("禁止联网"))

        def run(*flags):
            monkeypatch.setattr(sys, "argv", ["boss_apply.py", "--job", "产品经理", "--city", "深圳", *flags])
            boss.main()

        return SimpleNamespace(run=run, calls=calls, chats=chats, page=page, sleeps=sleeps,
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
    h.run("--count", "1")
    assert h.chats == ["a"]
    assert len(h.calls) == 3
    assert h.page.scrolled == 0
    assert h.sleeps == [30]
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
    assert h.sleeps == [3, 3]
    assert len(h.log()["pending"]) == len(h.log()["applied"]) == 1
    assert {"boss:a", "boss:b"} <= load_applied_index(tmp_path)
    assert "推荐：2｜复核：0｜跳过：0｜已沟通：1｜待核验：1" in h.queue()
    assert "| 公司a |" not in h.queue()
    h.run()
    assert len(h.calls) == 2 and h.chats == ["a", "b"]


def test_p0_extra_rest_every_five_successes(harness):
    h = harness([card(str(i)) for i in range(5)])
    h.run("--count", "5")
    assert h.sleeps == [30, 30, 30, 30, 30, 180]


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


@pytest.mark.parametrize("where", ["page", "chat"])
def test_blocked_exit_always_writes_queue(harness, where):
    h = harness([card("a"), card("b")], chat_statuses=["blocked"])
    if where == "page":
        h.page.blocked = "页面风控"
    with pytest.raises(SystemExit) as exc:
        h.run()
    assert exc.value.code == 1
    assert len(h.log()["failed"]) == 1
    assert h.log()["failed"][0]["status"] == "blocked"
    assert "停止原因" in h.queue()
    assert len(h.chats) == (1 if where == "chat" else 0)
    assert not h.sleeps  # 风控不等休息、不加载下一岗位


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
