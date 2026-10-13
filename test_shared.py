"""shared.py 公共函数单元测试（不依赖真实浏览器）"""

import json

import pytest

from shared import (
    classify_boss_chat_response,
    dedup_key,
    is_blacklisted,
    load_applied_index,
    load_log,
    parse_boss_job_id,
    parse_common_args,
    print_summary,
    record,
)

CITY_CODES = {"全国": "000", "深圳": "050090"}


# ---------- Boss 职位 ID / 沟通响应 ----------


@pytest.mark.parametrize("href", [
    "/job_detail/abcDEF012_-9.html",
    "/job_detail/abcDEF012_-9.html?securityId=xyz&lid=1",
    "job_detail/abcDEF012_-9.html?from=search",
    "https://www.zhipin.com/job_detail/abcDEF012_-9.html?ka=search#detail",
    "//www.zhipin.com/job_detail/abcDEF012_-9.html",
])
def test_parse_boss_job_id(href):
    assert parse_boss_job_id(href) == "abcDEF012_-9"


@pytest.mark.parametrize("href", [
    "", None, 123, "/job_detail/.html", "/job_detail/abc", "/job_detail/a/b.html",
    "/job_detail/abc.html/extra", "/company/abc.html",
    "https://www.zhipin.com/?next=/job_detail/abc.html",
    "/search#job_detail/abc.html", "javascript:/job_detail/abc.html", "http://[broken",
])
def test_parse_boss_job_id_invalid(href):
    assert parse_boss_job_id(href) == ""


@pytest.mark.parametrize("packet", [None, {}])
def test_classify_boss_chat_response_missing(packet):
    assert classify_boss_chat_response(packet) == ("pending", "未捕获接口响应")


def test_classify_boss_chat_response_success():
    assert classify_boss_chat_response({"http_status": 200, "code": 0, "message": "OK"}) == (
        "chat_started", "接口确认",
    )


@pytest.mark.parametrize("message", ["今日沟通已达上限", "操作频繁", "请完成验证", "账号异常"])
def test_classify_boss_chat_response_blocked(message):
    assert classify_boss_chat_response({"http_status": 403, "code": 7, "message": message}) == (
        "blocked", message,
    )


def test_classify_boss_chat_response_checks_message_before_log_truncation():
    message = "x" * 200 + "需要验证"
    assert classify_boss_chat_response({"http_status": 200, "code": 1, "message": message}) == (
        "blocked", message,
    )


@pytest.mark.parametrize("packet, reason", [
    ({"http_status": 200, "code": 5, "message": "岗位已关闭"}, "code=5 岗位已关闭"),
    ({"http_status": 500, "code": 0, "message": "服务错误"}, "code=0 服务错误"),
    ({"http_status": 200, "code": "0", "message": "OK"}, "code=0 OK"),
    ({"http_status": 200}, "code=None "),
])
def test_classify_boss_chat_response_failed(packet, reason):
    assert classify_boss_chat_response(packet) == ("failed", reason)


# ---------- dedup_key ----------


def test_dedup_key_normalizes_case_and_whitespace():
    assert dedup_key(" Tencent ", "  产品 经理 ") == dedup_key("tencent", "产品 经理")


def test_dedup_key_company_and_title_both_matter():
    assert dedup_key("A", "产品经理") != dedup_key("B", "产品经理")
    assert dedup_key("A", "产品经理") != dedup_key("A", "运营")


def test_dedup_key_empty_company_falls_back_to_title():
    # 公司缺失时仍能按标题去重，两条空公司同标题应相等
    assert dedup_key("", "产品经理") == dedup_key(None, "产品经理")


def test_dedup_key_all_empty_is_falsy():
    assert dedup_key("", "") == ""
    assert not dedup_key(None, None)


# ---------- load_applied_index ----------


def _write_log(path, applied):
    path.write_text(
        json.dumps({"applied": applied, "skipped": [], "failed": []}),
        encoding="utf-8",
    )


def test_load_applied_index_aggregates_all_logs(tmp_path):
    _write_log(
        tmp_path / "boss-深圳-log.json",
        [{"company": "腾讯", "job": "产品经理"}],
    )
    _write_log(
        tmp_path / "liepin-上海-x-log.json",
        [{"company": "阿里", "job": "运营"}],
    )
    idx = load_applied_index(tmp_path)
    assert dedup_key("腾讯", "产品经理") in idx
    assert dedup_key("阿里", "运营") in idx
    assert dedup_key("字节", "产品经理") not in idx


def test_load_applied_index_skips_empty_keys(tmp_path):
    _write_log(tmp_path / "yupao-x-log.json", [{"job": ""}])
    assert load_applied_index(tmp_path) == set()


def test_load_applied_index_tolerates_missing_and_bad_files(tmp_path):
    (tmp_path / "broken-log.json").write_text("{not json", encoding="utf-8")
    # 目录里没有合法 applied 记录也不应抛异常
    assert load_applied_index(tmp_path) == set()


def test_load_applied_index_missing_company_uses_title(tmp_path):
    _write_log(tmp_path / "51job-x-log.json", [{"job": "数据分析"}])
    idx = load_applied_index(tmp_path)
    assert dedup_key("", "数据分析") in idx


@pytest.mark.parametrize("bucket", ["applied", "pending"])
def test_load_applied_index_includes_boss_id_and_legacy_key(tmp_path, bucket):
    (tmp_path / "boss-深圳-log.json").write_text(json.dumps({bucket: [
        {"platform": "boss", "job_id": "job123", "company": "腾讯", "job": "产品经理"},
    ]}), encoding="utf-8")
    assert load_applied_index(tmp_path) == {"boss:job123", dedup_key("腾讯", "产品经理")}


def test_load_applied_index_pending_without_id_keeps_legacy_key(tmp_path):
    (tmp_path / "boss-x-log.json").write_text(json.dumps({"pending": [
        {"company": "腾讯", "job": "产品经理"},
    ]}), encoding="utf-8")
    assert load_applied_index(tmp_path) == {dedup_key("腾讯", "产品经理")}


def test_load_applied_index_id_does_not_require_company_or_title(tmp_path):
    _write_log(tmp_path / "boss-x-log.json", [{"job_id": "only-id"}])
    assert load_applied_index(tmp_path) == {"boss:only-id"}


def test_load_applied_index_excludes_unsubmitted_buckets(tmp_path):
    entry = {"job_id": "job123", "company": "腾讯", "job": "产品经理"}
    (tmp_path / "boss-x-log.json").write_text(json.dumps({
        "skipped": [entry], "failed": [entry], "dry_run": [entry], "review": [entry],
    }), encoding="utf-8")
    assert load_applied_index(tmp_path) == set()


# ---------- load_log / print_summary ----------


def test_load_log_adds_pending_to_legacy_file_without_losing_entries(tmp_path):
    path = tmp_path / "boss-x-log.json"
    entry = {"company": "腾讯", "job": "产品经理"}
    _write_log(path, [entry])
    log = load_log(path)
    assert log == {"applied": [entry], "pending": [], "review": [], "skipped": [], "failed": []}
    assert "pending" not in json.loads(path.read_text())  # 读取不改写旧文件


def test_load_log_preserves_existing_pending(tmp_path):
    path = tmp_path / "boss-x-log.json"
    path.write_text(json.dumps({"pending": [{"job_id": "job123"}]}), encoding="utf-8")
    assert load_log(path)["pending"] == [{"job_id": "job123"}]


def test_load_log_new_file_includes_pending(tmp_path):
    assert load_log(tmp_path / "new-log.json") == {
        "applied": [], "pending": [], "review": [], "skipped": [], "failed": [],
    }


def test_print_summary_pending_and_legacy_call(capsys, tmp_path):
    print_summary("Boss", 1, 2, tmp_path / "log.json", pending=3)
    assert "待核验: 3 份" in capsys.readouterr().out
    print_summary("其他平台", 1, 2, tmp_path / "log.json")
    assert "待核验: 0 份" in capsys.readouterr().out


# ---------- is_blacklisted ----------


def test_is_blacklisted_substring_case_insensitive():
    cfg = {"company_blacklist": ["外包", "ABC"]}
    assert is_blacklisted("某某外包公司", cfg)
    assert is_blacklisted("abc科技", cfg)
    assert not is_blacklisted("腾讯", cfg)


def test_is_blacklisted_empty_company_or_list():
    assert not is_blacklisted("", {"company_blacklist": ["外包"]})
    assert not is_blacklisted("腾讯", {})
    assert not is_blacklisted("腾讯", {"company_blacklist": []})


# ---------- parse_common_args ----------


def test_parse_common_args_defaults():
    args = parse_common_args(
        CITY_CODES,
        default_count=20,
        default_min_score=60,
        argv=["--job", "产品经理"],
    )
    assert args.job == "产品经理"
    assert args.city == "全国"
    assert args.count == 20
    assert args.min_score == 60
    assert args.dry_run is False
    assert args.fast is False


@pytest.mark.parametrize("flags, dry_run, fast", [
    (["--dry-run"], True, False),
    (["--fast"], False, True),
    (["--dry-run", "--fast"], True, True),
])
def test_parse_common_args_boss_debug_flags(flags, dry_run, fast):
    args = parse_common_args(CITY_CODES, argv=["--job", "产品经理", *flags])
    assert args.dry_run is dry_run
    assert args.fast is fast


def test_parse_common_args_overrides_and_default_city():
    args = parse_common_args(
        CITY_CODES,
        default_city="深圳",
        default_count=10,
        default_min_score=50,
        argv=["--job", "运营", "--count", "5", "--min-score", "0"],
    )
    assert args.city == "深圳"
    assert args.count == 5
    assert args.min_score == 0


def test_parse_common_args_requires_job():
    with pytest.raises(SystemExit):
        parse_common_args(CITY_CODES, default_count=20, default_min_score=60, argv=[])


# ---------- record ----------


def test_record_appends_and_persists(tmp_path):
    log_file = tmp_path / "t-log.json"
    log = {"applied": [], "skipped": [], "failed": []}
    record(log, log_file, "applied", {"company": "腾讯", "job": "产品"})
    record(log, log_file, "skipped", {"job": "低分岗", "score": 10})
    assert len(log["applied"]) == 1
    on_disk = json.loads(log_file.read_text(encoding="utf-8"))
    assert on_disk["applied"][0]["company"] == "腾讯"
    assert on_disk["skipped"][0]["score"] == 10
