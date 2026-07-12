#!/usr/bin/env python3
"""51job（前程无忧）自动投递脚本（DrissionPage版）

前置要求：51job 在线简历必须填写完整，否则点投递会被踢到简历向导页。
"""

import json
import time
from pathlib import Path
from urllib.parse import quote

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


# we.51job.com 的 jobArea 参数（已验证）
CITY_CODES = {
    "北京": "010000",
    "上海": "020000",
    "广州": "030200",
    "深圳": "040000",
    "杭州": "080200",
    # 更多城市码可从 we.51job.com 城市选择器抓取后补充
}


def get_cards(search_tab) -> list[dict]:
    """抓搜索页所有卡片元数据 + 投递按钮状态 + 校招标签"""
    return (
        search_tab.run_js("""
        return Array.from(document.querySelectorAll('.joblist-item')).map(c => {
            var sd = {};
            try {
                sd = JSON.parse(c.querySelector('[sensorsdata]')?.getAttribute('sensorsdata') || '{}');
            } catch(e) {}
            var link = c.querySelector('a')?.href || '';
            var btn = c.querySelector('button.btn.apply');
            // 公司名：优先 DOM 卡片内的公司元素，降级 sensorsdata（抓不到留空）
            var cname = (
                c.querySelector('.cname, .company-name, .company_name, [class*="cname"]')?.innerText
                || sd.companyName || sd.jobCompanyName || ''
            ).trim();
            // 校招标签：卡片内有纯文字"校招"的小标签元素
            var isCampus = Array.from(c.querySelectorAll('*')).some(e =>
                e.children.length === 0 && e.textContent.trim() === '校招'
            );
            return {
                jobId: sd.jobId || '',
                title: sd.jobTitle || '',
                salary: sd.jobSalary || '',
                area: sd.jobArea || '',
                year: sd.jobYear || '',
                degree: sd.jobDegree || '',
                company: cname,
                href: link,
                btnText: (btn?.innerText || '').trim(),
                isCampus: isCampus,
            };
        }).filter(x => x.jobId);
    """)
        or []
    )


def fetch_jd(detail_tab, href: str) -> str:
    """详情页取 JD 文本（.tCompanyPage 里有岗位职责 + 任职要求）"""
    try:
        detail_tab.get(href)
        time.sleep(2)
        return (
            detail_tab.run_js("""
            var el = document.querySelector('.tCompanyPage')
                  || document.querySelector('.job_msg')
                  || document.querySelector('.bmsg');
            return el ? el.innerText.slice(0, 3000) : '';
        """)
            or ""
        )
    except Exception:
        return ""


def click_apply_on_detail(detail_tab) -> str:
    """在详情页点'申请职位'按钮"""
    return (
        detail_tab.run_js("""
        // 找 "申请职位" 按钮
        var all = Array.from(document.querySelectorAll('button, a, div, span'));
        var btn = all.find(e =>
            e.children.length === 0 &&
            e.textContent.trim() === '申请职位'
        );
        if (!btn) {
            // 兼容：父元素是 button 而文字在子 span
            btn = all.find(e =>
                /^(BUTTON|A)$/.test(e.tagName) && e.innerText?.trim() === '申请职位'
            );
        }
        if (!btn) return 'not_found';
        btn.scrollIntoView({block:'center'});
        ['mousedown','mouseup','click'].forEach(ev =>
            btn.dispatchEvent(new MouseEvent(ev, {bubbles:true, cancelable:true, view:window}))
        );
        return 'clicked';
    """)
        or "js_null"
    )


def detail_apply_result(detail_tab) -> str:
    """读详情页 body，判定申请结果: success / resume_guide / pending"""
    try:
        url = detail_tab.url
    except Exception:
        return "pending"
    if "resume/guide" in url:
        return "resume_guide"
    body = detail_tab.run_js("return document.body.innerText.slice(0, 500);") or ""
    if "已申请" in body or "申请成功" in body or "投递成功" in body:
        return "success"
    return "pending"


def click_apply(search_tab, job_id: str) -> str:
    """按 jobId 定位卡片并点投递按钮。滚到视野内 + 派发真实 mouse event。"""
    return (
        search_tab.run_js(f"""
        var cards = document.querySelectorAll('.joblist-item');
        for (var c of cards) {{
            var sd = c.querySelector('[sensorsdata]');
            if (!sd) continue;
            try {{
                var d = JSON.parse(sd.getAttribute('sensorsdata'));
                if (String(d.jobId) === {json.dumps(str(job_id))}) {{
                    var btn = c.querySelector('button.btn.apply');
                    if (!btn) return 'no_button';
                    if (btn.disabled) return 'disabled';
                    c.scrollIntoView({{block:'center'}});
                    ['mousedown','mouseup','click'].forEach(ev =>
                        btn.dispatchEvent(new MouseEvent(ev, {{bubbles:true, cancelable:true, view:window}}))
                    );
                    return 'clicked';
                }}
            }} catch(e) {{}}
        }}
        return 'card_not_found';
    """)
        or "js_null"
    )


def read_button_state(search_tab, job_id: str) -> str:
    """回读 jobId 对应卡片当前按钮文案"""
    return (
        search_tab.run_js(f"""
        var cards = document.querySelectorAll('.joblist-item');
        for (var c of cards) {{
            var sd = c.querySelector('[sensorsdata]');
            if (!sd) continue;
            try {{
                var d = JSON.parse(sd.getAttribute('sensorsdata'));
                if (String(d.jobId) === {json.dumps(str(job_id))}) {{
                    return (c.querySelector('button.btn.apply')?.innerText || '').trim();
                }}
            }} catch(e) {{}}
        }}
        return '';
    """)
        or ""
    )


def main():
    cfg = load_config()
    args = parse_common_args(
        CITY_CODES,
        default_city="深圳",
        default_count=cfg["default_count"],
        default_min_score=cfg["min_score"],
    )

    if args.city not in CITY_CODES:
        print(f"❌ 未支持的城市：{args.city}。支持：{list(CITY_CODES)}")
        return
    city_code = CITY_CODES[args.city]
    search_url = (
        f"https://we.51job.com/pc/search?keyword={quote(args.job)}&jobArea={city_code}"
    )
    log_file = Path(__file__).parent / f"51job-{args.job[:10]}-log.json"

    log = load_log(log_file)
    applied_index = load_applied_index()
    seen_ids = set(
        str(e.get("jobId", "")) for e in log.get("applied", []) + log.get("skipped", [])
    )

    print(f"🎯 51job | 岗位: {args.job} | 城市: {args.city} | 目标: {args.count} 份")
    print(f"   最低评分: {args.min_score} | 已记录: {len(seen_ids)} 个 jobId")
    print("🔗 连接 Chrome（端口 9222）...")
    page = connect_chrome()
    search_tab = page.new_tab(search_url)
    time.sleep(5)

    if "login" in search_tab.url:
        print("⚠️  需先登录，登好后按 Enter...")
        input()
        search_tab.get(search_url)
        time.sleep(5)

    detail_tab = page.new_tab("about:blank")

    applied = 0
    skipped = 0
    page_num = 1

    try:
        while applied < args.count:
            # 搜索 tab 可能断连 → 重开
            try:
                search_tab.get(f"{search_url}&pageNum={page_num}")
            except Exception:
                print(f"  ⚠️  search_tab 断连，重开第 {page_num} 页")
                search_tab = page.new_tab(f"{search_url}&pageNum={page_num}")
            time.sleep(4)

            # 预热：滚到底再回顶（解决首屏卡片 Vue 水合时机）
            try:
                search_tab.run_js("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(2)
                search_tab.run_js("window.scrollTo(0, 0);")
                time.sleep(1)
            except Exception:
                pass  # 预热失败不影响主流程

            try:
                cards = get_cards(search_tab)
            except Exception:
                cards = []
            if not cards:
                print("没有更多岗位，停止")
                break

            to_process = [
                c
                for c in cards
                if c["jobId"] not in seen_ids and "已申请" not in c["btnText"]
            ]
            already_done = sum(1 for c in cards if "已申请" in c["btnText"])
            print(
                f"\n── 第 {page_num} 页 ── 卡片 {len(cards)}，待处理 {len(to_process)}，已投 {already_done}"
            )

            if not to_process:
                page_num += 1
                continue

            for c in to_process:
                if applied >= args.count:
                    break
                seen_ids.add(c["jobId"])

                company = c.get("company", "")
                key = dedup_key(company, c["title"])

                # 公司黑名单 → 直接跳过
                if is_blacklisted(company, cfg):
                    skipped += 1
                    record(
                        log,
                        log_file,
                        "skipped",
                        {
                            "jobId": c["jobId"],
                            "company": company,
                            "job": c["title"],
                            "reason": "公司黑名单",
                        },
                    )
                    print(f"[黑名单] {company} | {c['title'][:30]}")
                    continue

                # 跨运行去重 → 已投过则跳过（省一次详情页加载）
                if key and key in applied_index:
                    skipped += 1
                    record(
                        log,
                        log_file,
                        "skipped",
                        {
                            "jobId": c["jobId"],
                            "company": company,
                            "job": c["title"],
                            "reason": "已投过（跨运行去重）",
                        },
                    )
                    print(f"[已投] {c['title'][:30]}")
                    continue

                # 校招卡片直接跳过（避免浏览器加载应届生外链）
                if c.get("isCampus"):
                    skipped += 1
                    record(
                        log,
                        log_file,
                        "skipped",
                        {
                            "jobId": c["jobId"],
                            "job": c["title"],
                            "reason": "校招岗位（外链应届生求职网）",
                        },
                    )
                    print(f"[跳过] {c['title'][:30]} → 校招")
                    continue

                jd = fetch_jd(detail_tab, c["href"])
                try:
                    detail_url = detail_tab.url
                except Exception:
                    detail_url = ""

                # 外链岗位（跳到应届生求职网）→ 自动跳回 51job 搜索，继续下一个
                if "yingjiesheng.com" in detail_url:
                    try:
                        detail_tab.get("https://we.51job.com/pc/search")
                    except Exception:
                        pass
                    skipped += 1
                    record(
                        log,
                        log_file,
                        "skipped",
                        {
                            "jobId": c["jobId"],
                            "job": c["title"],
                            "reason": "外链(应届生求职网)→已跳回51job",
                        },
                    )
                    print(f"[跳过] {c['title'][:30]} → 应届生外链（已跳回51job）")
                    continue

                meta_line = f"{c['area']} {c['salary']} {c['year']} {c['degree']}"
                desc = meta_line + "\n" + jd
                score, reason = score_jd(c["title"], desc, cfg)
                print(
                    f"[{score:3d}分] {c['title'][:30]} ({c['salary']}/{c['area']}) → {reason}"
                )

                if score < args.min_score:
                    skipped += 1
                    record(
                        log,
                        log_file,
                        "skipped",
                        {
                            "jobId": c["jobId"],
                            "job": c["title"],
                            "score": score,
                            "reason": reason,
                        },
                    )
                    continue

                # 在详情页点"申请职位"（detail_tab 当前就在详情页）
                result = click_apply_on_detail(detail_tab)
                time.sleep(3)

                # 跳到简历向导 = 在线简历未完善
                state = detail_apply_result(detail_tab)
                if state == "resume_guide":
                    print("  ⚠️  被踢到简历向导 — 请先在 51job 完善在线简历")
                    return

                if state == "success":
                    applied += 1
                    if key:
                        applied_index.add(key)
                    record(
                        log,
                        log_file,
                        "applied",
                        {
                            "jobId": c["jobId"],
                            "company": company,
                            "job": c["title"],
                            "salary": c["salary"],
                            "area": c["area"],
                            "score": score,
                        },
                    )
                    print(f"  ✅ 已申请 ({applied}/{args.count})")
                else:
                    record(
                        log,
                        log_file,
                        "failed",
                        {
                            "jobId": c["jobId"],
                            "job": c["title"],
                            "reason": f"click={result}, state={state}",
                        },
                    )
                    print(f"  ❌ 未确认成功: click={result}, 状态={state}")

                time.sleep(2)

            page_num += 1
    finally:
        try:
            detail_tab.close()
        except Exception:
            pass

    print_summary("51job", applied, skipped, log_file)


if __name__ == "__main__":
    main()
