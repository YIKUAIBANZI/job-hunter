#!/usr/bin/env python3
"""猎聘自动投递（API 拦截版，参考 loks666/get_jobs 的策略）

核心策略：
1. 拦截猎聘搜索 XHR（com.liepin.searchfront4c.pc-search-job）的 JSON 响应
   直接拿 jobId/title/salary/company/hrName，不依赖 DOM 选择器
2. 在搜索结果卡片内直接点「聊一聊」，不打开 JD 详情页
   单页只 1 个核心请求，比"逐个开 JD 详情页"方案的请求量低 20 倍——这是合规关键
3. 点击前鼠标微调（中心 → +2px → -2px → 中心），模拟人工抖动

按钮文本：
- 「聊一聊」= 可投递
- 「继续聊」= 已投过，跳过

风控检测：URL 出现 safe.liepin.com / intercept / verifysms 立即终止
"""

import json as _json
import random
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

CITY_CODES = {
    "全国": "000",
    "北京": "010",
    "上海": "020",
    "广州": "050020",
    "深圳": "050090",
    "杭州": "070020",
    "南京": "070040",
    "成都": "280020",
    "武汉": "180020",
    "苏州": "070060",
    "天津": "030",
    "重庆": "040",
}

SEARCH_API = "com.liepin.searchfront4c.pc-search-job"
SEARCH_API_EXCLUDE = "cond-init"  # 排除参数初始化接口

JOB_CARD_SEL = ".job-card-pc-container"
CHAT_BTN_TEXT = "聊一聊"
DELIVERED_TEXT = "继续聊"


def is_blocked_url(url: str) -> bool:
    u = url or ""
    return "safe.liepin.com" in u or "intercept" in u or "verifysms" in u


def extract_cards_from_packet(packet) -> list[dict]:
    """从拦截的 API 响应 JSON 里提取卡片数据。"""
    try:
        body = packet.response.body
        if isinstance(body, str):
            body = _json.loads(body)
        data = body.get("data", {}) if isinstance(body, dict) else {}
        card_list = None
        # 兼容 data.data.jobCardList 和 data.jobCardList 两种结构
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, dict) and isinstance(inner.get("jobCardList"), list):
                card_list = inner["jobCardList"]
            elif isinstance(data.get("jobCardList"), list):
                card_list = data["jobCardList"]
        if not isinstance(card_list, list):
            return []
        out = []
        for item in card_list:
            job = item.get("job") or {}
            comp = item.get("comp") or {}
            recruiter = item.get("recruiter") or {}
            out.append(
                {
                    "jobId": job.get("jobId"),
                    "title": (job.get("title") or "").strip(),
                    "salary": job.get("salary") or "",
                    "eduReq": job.get("requireEduLevel") or "",
                    "expReq": job.get("requireWorkYears") or "",
                    "compName": comp.get("compName") or "",
                    "compIndustry": comp.get("compIndustry") or "",
                    "compScale": comp.get("compScale") or "",
                    "hrName": recruiter.get("recruiterName") or "",
                    "hrTitle": recruiter.get("recruiterTitle") or "",
                }
            )
        return out
    except Exception as e:
        print(f"  ⚠️  解析 API 响应失败: {e}")
        return []


def mouse_jiggle_click(tab, ele) -> bool:
    """鼠标在元素中心附近抖动后再点击。"""
    try:
        tab.actions.move_to(ele)
        tab.actions.move(2, 0)
        tab.actions.move(-2, 0)
        tab.actions.move_to(ele).click(ele)
        return True
    except Exception:
        # 降级为普通 click
        try:
            ele.click()
            return True
        except Exception:
            return False


def find_card_button(card):
    """在卡片内找「聊一聊/继续聊」按钮，返回 (button, text) 或 (None, '')。"""
    for sel in (
        "button.ant-btn-primary.ant-btn-round",
        "button.ant-btn",
        "a.btn-chat",
        "button",
    ):
        try:
            candidates = card.eles(sel, timeout=0.3)
        except Exception:
            continue
        for c in candidates:
            try:
                t = (c.text or "").strip()
            except Exception:
                continue
            if t in (CHAT_BTN_TEXT, DELIVERED_TEXT):
                return c, t
    return None, ""


def submit_on_card(tab, card_idx: int) -> str:
    """在第 card_idx 个卡片上投递。返回 applied/already/blocked/no_btn/error"""
    try:
        cards = tab.eles(JOB_CARD_SEL)
    except Exception:
        return "error"
    if card_idx >= len(cards):
        return "no_btn"
    card = cards[card_idx]

    # 滚动到卡片中心
    try:
        tab.run_js(
            "arguments[0].scrollIntoView({behavior:'instant',block:'center'})", card
        )
        time.sleep(0.4)
    except Exception:
        pass

    # 悬停到 HR 区域（触发按钮显示）
    try:
        hr_area = card.ele(".recruiter-info-box", timeout=0.5)
        if hr_area:
            tab.actions.move_to(hr_area)
            time.sleep(0.3)
    except Exception:
        pass

    btn, btn_text = find_card_button(card)
    if not btn:
        return "no_btn"
    if btn_text == DELIVERED_TEXT:
        return "already"
    if btn_text != CHAT_BTN_TEXT:
        return "no_btn"

    if is_blocked_url(tab.url):
        return "blocked"

    if not mouse_jiggle_click(tab, btn):
        return "error"

    time.sleep(random.uniform(1.8, 2.8))

    if is_blocked_url(tab.url):
        return "blocked"

    # 关闭可能弹出的聊天窗口（不同版本 class 不同，多试几个）
    for sel in (
        ".__im-basic__-dialog-close",
        ".im-dialog-close",
        ".ant-modal-close",
        "[class*='chat'] [class*='close']",
    ):
        try:
            close_btn = tab.ele(sel, timeout=0.3)
            if close_btn:
                close_btn.click()
                time.sleep(0.4)
                break
        except Exception:
            continue

    return "applied"


def main():
    cfg = load_config()
    args = parse_common_args(
        CITY_CODES,
        default_count=min(cfg["default_count"], 10),
        default_min_score=cfg["min_score"],
        count_help="投递数量（猎聘风控严，单日建议 ≤ 10）",
    )
    city_code = CITY_CODES.get(args.city, "000")
    log_file = Path(__file__).parent / f"liepin-{args.city}-{args.job}-log.json"

    log = load_log(log_file)
    applied_index = load_applied_index()
    applied = 0
    skipped = 0

    print(
        f"🎯 猎聘 API 版 | 岗位: {args.job} | 城市: {args.city} | 目标: {args.count} 份"
    )
    print("🔗 连接 Chrome 9222...")
    page = connect_chrome()

    page_num = 0
    while applied < args.count:
        url = (
            f"https://www.liepin.com/zhaopin/"
            f"?key={args.job}&dqs={city_code}&currentPage={page_num}"
        )
        print(f"\n── 第 {page_num + 1} 页 ──")
        print(f"📋 {url}")

        tab = page.new_tab()
        tab.listen.start(SEARCH_API)
        tab.get(url)

        # 立刻检查是否被拦截
        time.sleep(1.5)
        if is_blocked_url(tab.url):
            print(f"⛔ 风控拦截：{tab.url}")
            print("请完成验证并冷却数小时。脚本终止。")
            tab.listen.stop()
            tab.close()
            break

        # 等搜索接口的响应（跳过 cond-init）
        packet = None
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                p = tab.listen.wait(timeout=deadline - time.time())
            except Exception:
                p = None
            if not p:
                break
            if SEARCH_API_EXCLUDE in (p.url or ""):
                continue
            packet = p
            break
        tab.listen.stop()

        if not packet:
            print("⚠️  未拦截到搜索 API 响应")
            tab.close()
            break

        cards_data = extract_cards_from_packet(packet)
        print(f"📦 API 返回 {len(cards_data)} 个岗位")
        if not cards_data:
            tab.close()
            break

        # 等 DOM 渲染
        time.sleep(1.5)

        for i, data in enumerate(cards_data):
            if applied >= args.count:
                break
            title = data["title"]
            company = data["compName"]
            salary = data["salary"]

            # 公司黑名单 → 直接跳过
            if is_blacklisted(company, cfg):
                skipped += 1
                record(
                    log,
                    log_file,
                    "skipped",
                    {"company": company, "job": title, "reason": "公司黑名单"},
                )
                print(f"[黑名单] {company} | {title}")
                continue

            # 跨运行去重 → 已投过则跳过
            key = dedup_key(company, title)
            if key and key in applied_index:
                skipped += 1
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

            desc = " ".join(
                filter(
                    None,
                    [
                        data.get("eduReq", ""),
                        data.get("expReq", ""),
                        data.get("compIndustry", ""),
                        data.get("hrTitle", ""),
                    ],
                )
            )
            score, reason = score_jd(title, desc, cfg)
            print(f"[{score:3d}分] {company} | {title} | {salary} → {reason}")

            if score < args.min_score:
                skipped += 1
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
                continue

            result = submit_on_card(tab, i)
            if result == "applied":
                applied += 1
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
                        "hrName": data.get("hrName", ""),
                        "jobId": data.get("jobId"),
                    },
                )
                print(f"  ✅ 已投递 ({applied}/{args.count})")
            elif result == "already":
                skipped += 1
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
                print("  ⏭  已沟通过")
            elif result == "blocked":
                print(f"  ⛔ 风控触发：{tab.url}")
                record(log, log_file, "failed", {"job": title, "error": "blocked"})
                tab.close()
                return
            else:
                record(log, log_file, "failed", {"job": title, "error": result})
                print(f"  ❌ {result}")

            # 卡片间短暂停顿（不长，主要靠减少总请求数来合规）
            time.sleep(random.uniform(4, 8))

        tab.close()
        page_num += 1
        if applied < args.count:
            time.sleep(random.uniform(5, 10))

    print_summary("猎聘", applied, skipped, log_file)


if __name__ == "__main__":
    main()
