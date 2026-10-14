from datetime import datetime, timedelta

from boss_greet import build_greeting, load_greeted, pick_targets, save_greeted

NOW = datetime(2026, 10, 14, 18, 0).astimezone()


def entry(job_id, hours_ago, company="公司甲", job="AI研发实习生"):
    return {"job_id": job_id, "company": company, "job": job,
            "ts": (NOW - timedelta(hours=hours_ago)).isoformat()}


def test_build_greeting_strips_hello_and_fills_job():
    text = build_greeting({"greeting": "您好，我是27届本科。"}, entry("a", 1))
    assert text == "您好，关于贵司「AI研发实习生」岗位：我是27届本科。"


def test_build_greeting_custom_template():
    cfg = {"greeting": "期待沟通", "greeting_template": "{company}的{job}：{greeting}"}
    assert build_greeting(cfg, entry("a", 1)) == "公司甲的AI研发实习生：期待沟通"


def test_pick_targets_window_dedup_and_order():
    log = {"applied": [entry("old", 30), entry("b", 2), entry("a", 5), entry("done", 1),
                       {"company": "x", "job": "y", "ts": NOW.isoformat()},  # 无 job_id
                       entry("nocompany", 1, company="")]}
    picked = pick_targets(log, {"done": {}}, NOW, since_days=1)
    assert [e["job_id"] for e in picked] == ["a", "b"]


def test_pick_targets_ignores_pending_and_bad_ts():
    log = {"pending": [entry("p", 1)], "applied": [{**entry("bad", 1), "ts": "x"}]}
    assert pick_targets(log, {}, NOW, since_days=1) == []


def test_greeted_roundtrip_and_corrupt(tmp_path):
    (tmp_path / 'data').mkdir(parents=True, exist_ok=True)
    path = tmp_path / 'data' / "boss-greet.json"
    assert load_greeted(path) == {}
    save_greeted(path, {"a": {"status": "sent"}})
    assert load_greeted(path) == {"a": {"status": "sent"}}
    path.write_text("[1]")
    assert load_greeted(path) == {}


def test_build_greeting_strips_job_code():
    cfg = {"greeting": "期待沟通"}
    assert "「AI产品实习生」" in build_greeting(cfg, entry("a", 1, job="AI产品实习生（J11224）"))
    assert "「AI产品实习生」" in build_greeting(cfg, entry("a", 1, job="AI产品实习生(A239467)"))
