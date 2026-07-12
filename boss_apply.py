#!/usr/bin/env python3
"""Boss直聘自动投递脚本（DrissionPage版）"""

import json
import time
from pathlib import Path

from shared import (
    connect_chrome,
    dedup_key,
    is_blacklisted,
    load_applied_index,
    load_config,
    load_log,
    parse_common_args,
    print_summary,
    record,
    score_jd,
)

# 常用城市代码
CITY_CODES = {
    "全国": "100010000",
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "成都": "101270100",
    "武汉": "101200100",
    "南京": "101190100",
}


def main():
    cfg = load_config()
    args = parse_common_args(
        CITY_CODES,
        default_count=cfg["default_count"],
        default_min_score=cfg["min_score"],
    )
    city_code = CITY_CODES.get(args.city, "100010000")
    log_file = Path(__file__).parent / f"boss-{args.city}-log.json"

    log = load_log(log_file)
    applied_index = load_applied_index()
    applied_count = 0
    skipped_count = 0
    seen_titles = set()

    target_job = args.job
    target_count = args.count

    print(
        f"🎯 Boss直聘 | 岗位: {target_job} | 城市: {args.city} | 目标: {target_count} 份"
    )
    print("🔗 连接 Chrome（端口 9222）...")
    page = connect_chrome()

    search_url = (
        f"https://www.zhipin.com/web/geek/job?query={target_job}实习&city={city_code}"
    )
    print(f"📋 打开搜索页：{search_url}")
    page = page.new_tab(search_url)
    time.sleep(6)

    if "login" in page.url or "user/?ka" in page.url:
        print("⚠️  请先在浏览器登录后按 Enter...")
        input()
        page.get(search_url)
        time.sleep(4)

    print(f"🎯 开始投递，目标 {target_count} 份\n")

    page_num = 1
    while applied_count < target_count:
        print(f"── 第 {page_num} 页 ──────────────────────")
        time.sleep(3)

        cards = page.eles(".job-card-wrap")
        if not cards:
            print("未找到卡片，停止")
            break

        # 过滤已处理，只存 title（不缓存元素引用）
        pending_titles = []
        for c in cards:
            t = c.ele(".job-name")
            if t and t.text and t.text not in seen_titles:
                pending_titles.append(t.text)

        print(f"待处理: {len(pending_titles)} 个")
        if not pending_titles:
            break

        for title in pending_titles:
            if applied_count >= target_count:
                break
            seen_titles.add(title)

            # 实时查找公司/薪资（不缓存引用）
            company = (
                page.run_js(f"""
                var cards = document.querySelectorAll(".job-card-wrap");
                for (var c of cards) {{
                    var n = c.querySelector(".job-name");
                    if (n && n.textContent.trim() === {json.dumps(title)}) {{
                        var co = c.querySelector(".company-name");
                        return co ? co.textContent.trim() : "";
                    }}
                }}
                return "";
            """)
                or ""
            )
            salary = (
                page.run_js(f"""
                var cards = document.querySelectorAll(".job-card-wrap");
                for (var c of cards) {{
                    var n = c.querySelector(".job-name");
                    if (n && n.textContent.trim() === {json.dumps(title)}) {{
                        var s = c.querySelector(".job-salary");
                        return s ? s.textContent.trim() : "";
                    }}
                }}
                return "";
            """)
                or ""
            )

            # 公司黑名单 / 跨运行去重 → 点击前先拦掉，省一次 JD 加载
            key = dedup_key(company, title)
            if is_blacklisted(company, cfg):
                skipped_count += 1
                record(
                    log,
                    log_file,
                    "skipped",
                    {"company": company, "job": title, "reason": "公司黑名单"},
                )
                print(f"[黑名单] {company} | {title}")
                continue
            if key and key in applied_index:
                skipped_count += 1
                record(
                    log,
                    log_file,
                    "skipped",
                    {
                        "company": company,
                        "job": title,
                        "reason": "已投过（跨运行去重）",
                    },
                )
                print(f"[已投] {company} | {title}")
                continue

            try:
                # page级JS按岗位名定位并点击卡片
                page.run_js(f"""
                    var cards = document.querySelectorAll(".job-card-wrap");
                    for (var c of cards) {{
                        var n = c.querySelector(".job-name");
                        if (n && n.textContent.trim() === {json.dumps(title)}) {{
                            c.click(); break;
                        }}
                    }}
                """)
                time.sleep(3)

                desc_el = page.ele(".job-detail-body") or page.ele(".job-sec-text")
                desc = desc_el.text if desc_el else ""

                score, reason = score_jd(title, desc, cfg)
                print(f"[{score:3d}分] {company} | {title} | {salary} → {reason}")

                if score >= args.min_score:
                    # 检查是否已达今日沟通上限
                    btn_disabled = page.run_js(
                        'var b=document.querySelector(".op-btn-chat"); return b ? b.classList.contains("is-disabled") : false;'
                    )
                    if btn_disabled:
                        skipped_count += 1
                        record(
                            log,
                            log_file,
                            "skipped",
                            {
                                "company": company,
                                "job": title,
                                "score": score,
                                "reason": "已沟通过",
                            },
                        )
                        continue

                    # page级JS点击"立即沟通"
                    page.run_js(
                        'var b=document.querySelector(".op-btn-chat"); if(b) b.click();'
                    )
                    time.sleep(3)

                    time.sleep(2)
                    # 强制导回搜索页，不依赖弹窗行为
                    page.get(search_url)
                    time.sleep(4)

                    applied_count += 1
                    if key:
                        applied_index.add(key)
                    record(
                        log,
                        log_file,
                        "applied",
                        {
                            "company": company,
                            "job": title,
                            "salary": salary,
                            "score": score,
                        },
                    )
                    print(f"  ✅ 已投递 ({applied_count}/{target_count})")
                    time.sleep(2)
                else:
                    skipped_count += 1
                    record(
                        log,
                        log_file,
                        "skipped",
                        {
                            "company": company,
                            "job": title,
                            "score": score,
                            "reason": reason,
                        },
                    )

            except Exception as e:
                err = str(e)[:120]
                print(f"  ❌ 失败: {err}")
                record(log, log_file, "failed", {"job": title, "error": err})
                time.sleep(2)

        # 滚动到底部触发无限加载
        if applied_count < target_count:
            prev_count = len(page.eles(".job-card-wrap"))
            page.run_js("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(3)
            new_count = len(page.eles(".job-card-wrap"))
            if new_count <= prev_count:
                print("没有加载到新岗位，停止")
                break
            print(f"加载更多：{prev_count} → {new_count} 个")
            page_num += 1
        else:
            break

    print_summary("Boss直聘", applied_count, skipped_count, log_file)


if __name__ == "__main__":
    main()
