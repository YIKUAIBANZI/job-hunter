"""shared.py 公共函数单元测试（不依赖真实浏览器）"""

import json

import pytest

from shared import (
    dedup_key,
    is_blacklisted,
    load_applied_index,
    parse_common_args,
    record,
)

CITY_CODES = {"全国": "000", "深圳": "050090"}


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
