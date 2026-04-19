#!/usr/bin/env python3
"""51job（前程无忧）自动投递脚本（DrissionPage版）

前置要求：51job 在线简历必须填写完整，否则点投递会被踢到简历向导页。
"""

import argparse
import json
import time
from pathlib import Path
from urllib.parse import quote

from DrissionPage import ChromiumPage, ChromiumOptions

from shared import load_config, load_log, save_log, score_jd


# we.51job.com 的 jobArea 参数（已验证）
CITY_CODES = {
    "北京": "010000",
    "上海": "020000",
    "广州": "030200",
    "深圳": "040000",
    "杭州": "080200",
    # 更多城市码可从 we.51job.com 城市选择器抓取后补充
}


def parse_args(cfg: dict):
    p = argparse.ArgumentParser()
    p.add_argument("--job", required=True, help="搜索岗位名")
    p.add_argument(
        "--city",
        default="深圳",
        help=f"城市（暂支持：{'/'.join(CITY_CODES)}）",
    )
    p.add_argument("--count", type=int, default=cfg["default_count"], help="投递数量")
    p.add_argument(
        "--min-score",
        type=int,
        default=cfg["min_score"],
        help="最低评分阈值（0=全投不过滤）",
    )
    return p.parse_args()


def get_cards(search_tab) -> list[dict]:
    """抓搜索页所有卡片元数据 + 投递按钮状态"""
    return (
        search_tab.run_js("""
        return Array.from(document.querySelectorAll('.joblist-item')).map(c => {
            var sd = {};
            try {
                sd = JSON.parse(c.querySelector('[sensorsdata]')?.getAttribute('sensorsdata') || '{}');
            } catch(e) {}
            var link = c.querySelector('a')?.href || '';
            var btn = c.querySelector('button.btn.apply');
            return {
                jobId: sd.jobId || '',
                title: sd.jobTitle || '',
                salary: sd.jobSalary || '',
                area: sd.jobArea || '',
                year: sd.jobYear || '',
                degree: sd.jobDegree || '',
                href: link,
                btnText: (btn?.innerText || '').trim(),
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
    args = parse_args(cfg)

    if args.city not in CITY_CODES:
        print(f"❌ 未支持的城市：{args.city}。支持：{list(CITY_CODES)}")
        return
    city_code = CITY_CODES[args.city]
    search_url = (
        f"https://we.51job.com/pc/search?keyword={quote(args.job)}&jobArea={city_code}"
    )
    log_file = Path(__file__).parent / f"51job-{args.job[:10]}-log.json"

    log = load_log(log_file)
    seen_ids = set(
        str(e.get("jobId", "")) for e in log.get("applied", []) + log.get("skipped", [])
    )

    print(f"🎯 51job | 岗位: {args.job} | 城市: {args.city} | 目标: {args.count} 份")
    print(f"   最低评分: {args.min_score} | 已记录: {len(seen_ids)} 个 jobId")
    print("🔗 连接 Chrome（端口 9222）...")
    opts = ChromiumOptions().set_local_port(9222)
    page = ChromiumPage(addr_or_opts=opts)
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
            search_tab.get(f"{search_url}&pageNum={page_num}")
            time.sleep(4)

            # 预热：滚到底再回顶，逼所有卡片完成 Vue 水合（解决首屏卡片点击失效）
            search_tab.run_js("""
                window.scrollTo(0, document.body.scrollHeight);
            """)
            time.sleep(2)
            search_tab.run_js("window.scrollTo(0, 0);")
            time.sleep(1)

            cards = get_cards(search_tab)
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

                jd = fetch_jd(detail_tab, c["href"])
                try:
                    detail_url = detail_tab.url
                except Exception:
                    detail_url = ""

                # 外链岗位（跳到应届生求职网）直接跳过
                if "yingjiesheng.com" in detail_url:
                    skipped += 1
                    log["skipped"].append(
                        {
                            "jobId": c["jobId"],
                            "job": c["title"],
                            "reason": "外链(应届生求职网)",
                        }
                    )
                    save_log(log, log_file)
                    print(f"[跳过] {c['title'][:30]} → 应届生外链")
                    continue

                meta_line = f"{c['area']} {c['salary']} {c['year']} {c['degree']}"
                desc = meta_line + "\n" + jd
                score, reason = score_jd(c["title"], desc, cfg)
                print(
                    f"[{score:3d}分] {c['title'][:30]} ({c['salary']}/{c['area']}) → {reason}"
                )

                if score < args.min_score:
                    skipped += 1
                    log["skipped"].append(
                        {
                            "jobId": c["jobId"],
                            "job": c["title"],
                            "score": score,
                            "reason": reason,
                        }
                    )
                    save_log(log, log_file)
                    continue

                result = click_apply(search_tab, c["jobId"])
                time.sleep(3)

                # 跳到简历向导 = 在线简历未完善
                try:
                    current_url = search_tab.url
                except Exception:
                    print("  ⚠️  search_tab 连接中断，重开搜索页")
                    search_tab = page.new_tab(search_url)
                    time.sleep(4)
                    continue
                if "resume/guide" in current_url:
                    print("  ⚠️  被踢到简历向导 — 请先在 51job 完善在线简历")
                    print("     当前卡片未投递，流程终止")
                    return

                state = read_button_state(search_tab, c["jobId"])
                # 首次未生效 → 再点一次
                if "已申请" not in state and result == "clicked":
                    time.sleep(1)
                    click_apply(search_tab, c["jobId"])
                    time.sleep(3)
                    state = read_button_state(search_tab, c["jobId"])

                if "已申请" in state:
                    applied += 1
                    log["applied"].append(
                        {
                            "jobId": c["jobId"],
                            "job": c["title"],
                            "salary": c["salary"],
                            "area": c["area"],
                            "score": score,
                        }
                    )
                    save_log(log, log_file)
                    print(f"  ✅ 已投递 ({applied}/{args.count})")
                else:
                    log["failed"].append(
                        {
                            "jobId": c["jobId"],
                            "job": c["title"],
                            "external": external_tag or "",
                            "reason": f"click={result}, state={state!r}",
                        }
                    )
                    save_log(log, log_file)
                    print(f"  ❌ 未确认成功: click={result}, 按钮={state!r}")

                time.sleep(2)

            page_num += 1
    finally:
        try:
            detail_tab.close()
        except Exception:
            pass

    print(
        f"""
╔══════════════════════════════════╗
  51job 投递完成
  成功: {applied} 份  跳过: {skipped} 份
  日志: {log_file}
╚══════════════════════════════════╝
"""
    )


if __name__ == "__main__":
    main()
