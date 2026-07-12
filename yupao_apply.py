#!/usr/bin/env python3
"""鱼泡直聘自动投递脚本（DrissionPage版）"""

import json
import time
from pathlib import Path
from urllib.parse import quote

from shared import (
    connect_chrome,
    dedup_key,
    load_applied_index,
    load_config,
    load_log,
    parse_common_args,
    print_summary,
    record,
    score_jd,
)


CITY_CODES = {
    "全国": "a1c0",
    "北京": "a2c0",
    "上海": "a25c0",
    "天津": "a27c0",
    "重庆": "a32c0",
    "合肥": "a3401c0",
    "福州": "a53c0",
    "厦门": "a60c0",
    "兰州": "a62c0",
    "广州": "a76c0",
    "深圳": "a77c0",
    "东莞": "a79c0",
    "佛山": "a80c0",
    "惠州": "a82c0",
    "珠海": "a96c0",
    "南宁": "a97c0",
    "贵阳": "a111c0",
    "海口": "a120c0",
    "三亚": "a121c0",
    "石家庄": "a138c0",
    "郑州": "a149c0",
    "洛阳": "a150c0",
    "哈尔滨": "a167c0",
    "武汉": "a180c0",
    "长沙": "a197c0",
    "长春": "a211c0",
    "南京": "a220c0",
    "苏州": "a221c0",
    "无锡": "a222c0",
    "常州": "a223c0",
    "南通": "a226c0",
    "扬州": "a231c0",
    "南昌": "a233c0",
    "沈阳": "a244c0",
    "大连": "a245c0",
    "呼和浩特": "a258c0",
    "济南": "a283c0",
    "青岛": "a284c0",
    "烟台": "a297c0",
    "太原": "a300c0",
    "西安": "a311c0",
    "成都": "a322c0",
    "绵阳": "a323c0",
    "拉萨": "a344c0",
    "乌鲁木齐": "a351c0",
    "昆明": "a367c0",
    "杭州": "a383c0",
    "宁波": "a388c0",
    "温州": "a391c0",
}


def get_card_titles(page) -> list[str]:
    """从左侧列表收集真实岗位卡片标题（div.acbab 是实际岗位卡片）"""
    cards = page.run_js("""
        return Array.from(document.querySelectorAll('div.acbab'))
            .map(d => d.innerText.trim().split('\\n')[0].trim())
            .filter(t => t.length > 0);
    """)
    return cards or []


def click_card_by_title(page, title: str) -> bool:
    """按标题点击左侧卡片，触发右侧详情加载"""
    return page.run_js(f"""
        var cards = Array.from(document.querySelectorAll('div.acbab'));
        var card = cards.find(d => d.innerText.trim().split('\\n')[0].trim() === {json.dumps(title)});
        if (card) {{ card.click(); return true; }}
        return false;
    """)


def get_right_panel_text(page) -> str:
    """读取右侧详情面板的JD文本"""
    time.sleep(3)
    return (
        page.run_js("""
        // 找右侧面板（排除左侧列表区域）
        var detail = document.querySelector('.job-detail, .detail-wrap, .right-content, main .detail, [class*="detail"]');
        if (detail) return detail.innerText.slice(0, 3000);
        // 降级：取页面中段最长文本块
        var divs = Array.from(document.querySelectorAll('div'))
            .filter(d => d.children.length < 5 && d.innerText && d.innerText.length > 100);
        divs.sort((a, b) => b.innerText.length - a.innerText.length);
        return divs.length ? divs[0].innerText.slice(0, 3000) : document.body.innerText.slice(0, 3000);
    """)
        or ""
    )


def click_send_resume(page) -> tuple[bool, str]:
    """点击右侧投递按钮，优先'发送简历'，降级'聊一聊'。返回 (成功, 方式)"""
    result = page.run_js("""
        var all = Array.from(document.querySelectorAll('*'));
        var targets = ['发送简历', '聊一聊'];
        for (var text of targets) {
            var btns = all.filter(e =>
                e.children.length === 0 &&
                e.textContent.trim() === text &&
                getComputedStyle(e).cursor === 'pointer'
            );
            if (!btns.length) {
                btns = all.filter(e =>
                    e.children.length === 0 && e.textContent.trim() === text
                );
            }
            if (btns.length) { btns[0].click(); return text; }
        }
        return null;
    """)
    if result:
        return True, result
    return False, ""


def handle_modal(page) -> bool:
    """处理点击'发送简历'后可能出现的确认弹窗"""
    time.sleep(1.5)
    return page.run_js("""
        var keywords = ['确认发送', '确认', '发送', '确定', '投递'];
        for (var kw of keywords) {
            var btns = Array.from(document.querySelectorAll('button, [class*="btn"], [class*="confirm"]'));
            var b = btns.find(e => e.textContent.trim() === kw);
            if (b && getComputedStyle(b).display !== 'none') {
                b.click();
                return true;
            }
        }
        return false;
    """)


def scroll_for_more(page, current_count: int) -> int:
    """滚动左侧列表加载更多"""
    page.run_js("""
        var list = document.querySelector('.job-list, .list-wrap, [class*="job-list"]');
        if (list) list.scrollTop = list.scrollHeight;
        else window.scrollTo(0, document.body.scrollHeight);
    """)
    time.sleep(3)
    new_count = len(
        page.run_js("return Array.from(document.querySelectorAll('div.acbab'));") or []
    )
    return new_count


def main():
    cfg = load_config()
    args = parse_common_args(
        CITY_CODES,
        default_count=cfg["default_count"],
        default_min_score=cfg["min_score"],
    )
    target_count = args.count
    city_code = CITY_CODES.get(args.city, "a1c0")
    search_url = f"https://www.yupao.com/topic/{city_code}/?keywords={quote(args.job)}"
    log_file = Path(__file__).parent / f"yupao-{args.job[:10]}-log.json"

    log = load_log(log_file)
    applied_index = load_applied_index()
    seen_titles = set(
        e.get("job", "") for e in log.get("applied", []) + log.get("skipped", [])
    )

    applied_count = 0
    skipped_count = 0

    print(
        f"🎯 鱼泡直聘 | 岗位: {args.job} | 城市: {args.city} | 目标: {target_count} 份"
    )
    print("🔗 连接 Chrome（端口 9222）...")
    page = connect_chrome()
    page = page.new_tab(search_url)
    time.sleep(6)

    if "login" in page.url or "safe/verify" in page.url:
        print("⚠️  请先在浏览器登录后按 Enter...")
        input()
        page.get(search_url)
        time.sleep(5)

    print(f"🎯 开始投递，目标 {target_count} 份\n")

    while applied_count < target_count:
        titles = get_card_titles(page)
        new_titles = [t for t in titles if t not in seen_titles]
        print(f"列表共 {len(titles)} 个，未处理 {len(new_titles)} 个")

        if not new_titles:
            new_count = scroll_for_more(page, len(titles))
            if new_count <= len(titles):
                print("没有更多岗位，停止")
                break
            print(f"加载更多：{len(titles)} → {new_count}")
            continue

        for title in new_titles:
            if applied_count >= target_count:
                break
            seen_titles.add(title)

            # 鱼泡左列表卡片不含公司名 → 只能按标题跨运行去重
            key = dedup_key("", title)
            if key and key in applied_index:
                skipped_count += 1
                record(
                    log,
                    log_file,
                    "skipped",
                    {"job": title, "reason": "已投过（跨运行去重）"},
                )
                print(f"[已投] {title[:30]}")
                continue

            clicked = click_card_by_title(page, title)
            if not clicked:
                print(f"  ⚠️  点击失败: {title[:20]}")
                continue

            desc = get_right_panel_text(page)
            score, reason = score_jd(title, desc, cfg)

            print(f"[{score:3d}分] {title[:30]} → {reason}")

            if score < args.min_score:
                skipped_count += 1
                record(
                    log,
                    log_file,
                    "skipped",
                    {"job": title, "score": score, "reason": reason},
                )
                continue

            ok, action = click_send_resume(page)
            if not ok:
                need_login = page.run_js(
                    "return document.body.innerText.includes('点击登录');"
                )
                if need_login:
                    print("  ⚠️  检测到未登录，请登录后按 Enter...")
                    input()
                    click_card_by_title(page, title)
                    time.sleep(2)
                    ok, action = click_send_resume(page)

            if not ok:
                print("  ⚠️  未找到投递按钮，跳过")
                record(
                    log,
                    log_file,
                    "skipped",
                    {"job": title, "score": score, "reason": "按钮未找到"},
                )
                skipped_count += 1
                continue

            page_disconnected = False
            try:
                handle_modal(page)
            except Exception:
                page_disconnected = True  # 页面跳转/关闭视为投递成功
            time.sleep(2)

            applied_count += 1
            if key:
                applied_index.add(key)
            record(
                log,
                log_file,
                "applied",
                {"job": title, "score": score, "action": action},
            )
            print(f"  ✅ 已投递 [{action}] ({applied_count}/{target_count})")

            if page_disconnected:
                # 重新导航回搜索页继续
                time.sleep(1)
                page.get(search_url)
                time.sleep(5)
            else:
                time.sleep(2)

    print_summary("鱼泡直聘", applied_count, skipped_count, log_file)


if __name__ == "__main__":
    main()
