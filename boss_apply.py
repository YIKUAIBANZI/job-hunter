#!/usr/bin/env python3
"""Boss直聘自动投递脚本（DrissionPage版）"""

import json
import random
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from shared import (
    BOSS_JOB_ID_PATTERN,
    classify_boss_chat_response,
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

# 调试快模式不改变接口核验窗口；默认保留慢节奏。
CHAT_TARGET = "friend/add"
CHAT_RESPONSE_TIMEOUT = 8
CHAT_INTERVAL = (30, 90)
FAST_INTERVAL = (3, 6)
SUCCESS_REST_EVERY = 5
SUCCESS_REST = (180, 300)
BROWSE_JITTER = 0.25

# 一次读取整页卡片；与纯函数共用职位路径正则，避免两套 ID 提取规则漂移。
CARD_SNAPSHOT_JS = """
    const pattern = new RegExp(arguments[0]);
    return Array.from(document.querySelectorAll('.job-card-wrap')).map(c => {
        let job_id = '';
        for (const a of c.querySelectorAll('a[href]')) {
            try {
                const url = new URL(a.getAttribute('href'), location.href);
                if (!['http:', 'https:'].includes(url.protocol)) continue;
                const match = url.pathname.match(pattern);
                if (match) { job_id = match[1]; break; }
            } catch (_) {}
        }
        const text = selector => c.querySelector(selector)?.textContent.trim() || '';
        return {job_id, title: text('.job-name'),
                company: text('.company-name') || text('.boss-name'), salary: text('.job-salary')};
    });
"""


def browse_sleep(seconds):
    """浏览/读取 JD 的等待加 ±25% 抖动。"""
    time.sleep(random.uniform(
        seconds * (1 - BROWSE_JITTER), seconds * (1 + BROWSE_JITTER)
    ))


DETAIL_TITLE_JS = """
    const h = document.querySelector('.job-detail-header .job-name')
        || document.querySelector('.job-detail-info .job-name');
    return h ? h.textContent.trim() : '';
"""


def wait_detail_title(page, title, timeout=6):
    """等右侧详情标题与卡片标题一致；超时返回 False。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (page.run_js(DETAIL_TITLE_JS) or "") == title.strip():
            return True
        time.sleep(0.5)
    return False


def page_block_reason(page):
    """只检测风控 URL 和可见验证组件/弹层，不扫描 JD 正文中的“验证”等词。"""
    # 只看路径：登录后正常列表页也会带 ?_security_check= 参数
    path = urlsplit(page.url).path.lower()
    if any(word in path for word in ("verify", "security", "captcha")):
        return "页面跳转至验证/安全检查"
    return page.run_js("""
        const visible = e => e.getClientRects().length > 0 &&
            getComputedStyle(e).visibility !== 'hidden' &&
            getComputedStyle(e).display !== 'none';
        const widgets = document.querySelectorAll(
            '.verify-dialog, .verify-modal, .verify-wrap, .captcha-dialog, ' +
            '.geetest_panel, .nc-container, [id*="captcha"], [class*="captcha"], ' +
            'iframe[src*="verify"], iframe[src*="security"], iframe[src*="captcha"]');
        for (const e of widgets) {
            if (visible(e)) return (e.innerText || '页面出现验证组件').slice(0, 200);
        }
        const dialogs = document.querySelectorAll(
            '[role="dialog"], .dialog-container, .dialog-wrap, .dialog, .modal, .boss-popup');
        for (const e of dialogs) {
            const text = e.innerText || '';
            if (visible(e) && /验证|上限|频繁|异常/.test(text)) return text.slice(0, 200);
        }
        return '';
    """) or ""


def wait_chat_response(page, evidence):
    """收集本次点击后 8 秒内的命中包；任一风控响应立即终止等待。"""
    status, reason = classify_boss_chat_response(None)
    deadline = time.monotonic() + CHAT_RESPONSE_TIMEOUT
    timeout = CHAT_RESPONSE_TIMEOUT
    while timeout > 0:
        packet = page.listen.wait(timeout=timeout, raise_err=False)
        if not packet:
            break
        response = packet.response
        body = response.body
        if isinstance(body, (str, bytes)):
            try:
                body = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                body = {}
        body = body if isinstance(body, dict) else {}
        info = {
            "url": packet.url,
            "http_status": response.status,
            "code": body.get("code"),
            "message": body.get("message"),
        }
        # 原样保留 URL、HTTP 状态、code；message 仅日志截断，判定使用全文。
        evidence["packets"].append({
            **info,
            "message": info["message"][:200] if isinstance(info["message"], str) else info["message"],
        })
        result, result_reason = classify_boss_chat_response(info)
        if result == "blocked":
            return result, result_reason
        if result == "chat_started" or status != "chat_started":
            status, reason = result, result_reason
        timeout = deadline - time.monotonic()
    return status, reason


def start_chat(page, job_id):
    """先监听再点击；点击结果或核验过程异常时保守记 pending，避免重复提交。"""
    evidence = {"target": CHAT_TARGET, "packets": []}
    clicked = False
    listening = False
    status, reason = "failed", "未点击立即沟通"
    try:
        page.listen.start(CHAT_TARGET)
        listening = True
        # JS 执行若异常，无法证明点击未发生，按已尝试处理。
        clicked = True
        # 按钮 ka="cpc_job_list_chat_<job_id>"，点击前再核一次是不是当前岗位
        result = page.run_js("""
            const b = document.querySelector('.op-btn-chat');
            if (!b || b.classList.contains('is-disabled') || b.disabled) return 'disabled';
            if (!(b.getAttribute('ka') || '').endsWith(arguments[0])) return 'mismatch';
            b.click();
            return 'clicked';
        """, job_id)
        clicked = result == "clicked"
        if clicked:
            status, reason = wait_chat_response(page, evidence)
            # 页面兜底：弹出"已向BOSS发送消息"也算成功证据，然后点"留在此页"关掉
            confirmed = page.run_js("""
                const d = document.querySelector('.greet-boss-container');
                return !!d && /已向BOSS发送消息/.test(d.innerText || '');
            """)
            evidence["page_confirmed"] = bool(confirmed)
            if confirmed and status == "pending":
                status, reason = "chat_started", "页面弹窗确认"
            page.run_js("const c=document.querySelector('.greet-boss-container .cancel-btn'); if(c) c.click();")
        elif result == "mismatch":
            reason = "沟通按钮不属于当前岗位，未点击"
        else:
            reason = "立即沟通按钮不存在或不可用"
    except Exception as e:
        evidence["error"] = str(e)[:200]
        status = "pending" if clicked else "failed"
        reason = "点击后结果未知" if clicked else "监听启动失败，未点击"
    finally:
        # 必须在任何返回搜索页/休息之前检查，不能把验证页面刷掉。
        try:
            evidence["page_url"] = page.url
            block_reason = page_block_reason(page)
            if block_reason:
                evidence["page_blocked"] = block_reason
                status, reason = "blocked", block_reason
        except Exception as e:
            evidence["page_check_error"] = str(e)[:200]
        if listening:
            try:
                page.listen.stop()
            except Exception as e:
                evidence["listen_stop_error"] = str(e)[:200]
    return status, reason, evidence, clicked


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
    pending_count = 0
    skipped_count = 0
    dry_run_count = 0
    seen_job_ids = set()
    target_count = args.count

    def summary():
        print_summary("Boss直聘", applied_count, skipped_count, log_file, pending=pending_count)
        if args.dry_run:
            print(f"🧪 dry-run 已评分: {dry_run_count} 份（仅打印，未发起沟通）")

    def stop_blocked(reason, entry, evidence):
        record(log, log_file, "failed", {
            **entry, "status": "blocked", "reason": reason,
            "evidence": evidence, "ts": datetime.now().astimezone().isoformat(),
        })
        print(f"\n🛑🛑 检测到风控，立即终止整个脚本：{reason}\n")
        summary()
        raise SystemExit(1)

    def check_page(entry):
        reason = page_block_reason(page)
        if reason:
            stop_blocked(reason, entry, {"packets": [], "page_url": page.url, "page_blocked": reason})

    print(f"🎯 Boss直聘 | 岗位: {args.job} | 城市: {args.city} | 目标: {target_count} 份")
    if args.dry_run:
        print("🧪 dry-run：只浏览和打分，--count 为评分数量上限")
    if args.fast:
        print("⚠️  --fast 调试模式：沟通间隔及额外休息均为 3–6 秒")
    print("🔗 连接 Chrome（端口 9222）...")
    page = connect_chrome()

    search_url = f"https://www.zhipin.com/web/geek/job?query={args.job}实习&city={city_code}"
    print(f"📋 打开搜索页：{search_url}")
    page = page.new_tab(search_url)
    browse_sleep(6)
    check_page({"platform": "boss"})

    if "login" in page.url or "user/?ka" in page.url:
        print("⚠️  请先在浏览器登录后按 Enter...")
        input()
        page.get(search_url)
        browse_sleep(4)

    print(f"🎯 开始{'试跑' if args.dry_run else '投递'}，目标 {target_count} 份\n")
    page_num = 1
    while (dry_run_count if args.dry_run else applied_count) < target_count:
        print(f"── 第 {page_num} 页 ──────────────────────")
        browse_sleep(3)
        check_page({"platform": "boss"})
        cards = page.run_js(CARD_SNAPSHOT_JS, BOSS_JOB_ID_PATTERN) or []
        if not cards:
            print("未找到卡片，停止")
            break

        # 无 ID 卡片用 无ID:公司|标题 占位，避免无限滚动时每页重复记 skipped
        pending_cards = [c for c in cards if (c["job_id"] or f"无ID:{c['company']}|{c['title']}") not in seen_job_ids]
        print(f"待处理: {len(pending_cards)} 个")
        for card in pending_cards:
            if (dry_run_count if args.dry_run else applied_count) >= target_count:
                break
            job_id, title = card["job_id"], card["title"]
            company, salary = card["company"], card["salary"]
            entry = {"platform": "boss", "job_id": job_id, "company": company,
                     "job": title, "salary": salary}
            seen_key = job_id or f"无ID:{company}|{title}"
            if seen_key in seen_job_ids:
                continue
            seen_job_ids.add(seen_key)
            key = dedup_key(company, title)
            boss_key = f"boss:{job_id}"

            skip_reason = ""
            if not job_id:
                skip_reason = "无职位ID"
            elif is_blacklisted(company, cfg):
                skip_reason = "公司黑名单"
            elif boss_key in applied_index or (key and key in applied_index):
                skip_reason = "已投过或待核验（跨运行去重）"
            if skip_reason:
                skipped_count += 1
                record(log, log_file, "skipped", {**entry, "reason": skip_reason})
                print(f"⏭️  [{skip_reason}] {company} | {title} | ID={job_id}")
                continue

            try:
                check_page(entry)
                # 按 href 中的完整职位 ID 实时定位卡片，绝不按同名标题匹配。
                link = page.ele(
                    f'css:.job-card-wrap a[href*="job_detail/{job_id}.html"]', timeout=2
                )
                if not link:
                    raise RuntimeError(f"未找到职位卡片: {job_id}")
                # JS 点击不会切换右侧详情，必须原生点击 .job-card-box
                link.parent("css:.job-card-wrap").ele("css:.job-card-box").click()
                browse_sleep(2)
                check_page(entry)
                # 详情标题必须对上当前卡片，否则后面的"立即沟通"会发给别的岗位
                if not wait_detail_title(page, title):
                    skipped_count += 1
                    record(log, log_file, "skipped", {**entry, "reason": "详情未切换到该岗位"})
                    print(f"⏭️  [详情未切换] {company} | {title}")
                    continue
                desc_el = page.ele(".job-detail-body") or page.ele(".job-sec-text")
                if not desc_el:
                    raise RuntimeError("未找到岗位 JD，未发起沟通")
                score, reason = score_jd(title, desc_el.text, cfg)
                entry["score"] = score
                print(f"[{score:3d}分] {company} | {title} | {salary} | ID={job_id} → {reason}")

                if args.dry_run:
                    dry_run_count += 1
                    continue
                if score < args.min_score:
                    skipped_count += 1
                    record(log, log_file, "skipped", {**entry, "reason": reason})
                    continue

                btn_disabled = page.run_js(
                    'var b=document.querySelector(".op-btn-chat"); return b ? b.classList.contains("is-disabled") : false;'
                )
                if btn_disabled:
                    skipped_count += 1
                    record(log, log_file, "skipped", {**entry, "reason": "已沟通过"})
                    continue

                status, reason, evidence, clicked = start_chat(page, job_id)
                if status == "blocked":
                    stop_blocked(reason, entry, evidence)
                bucket = {"chat_started": "applied", "pending": "pending", "failed": "failed"}[status]
                record(log, log_file, bucket, {
                    **entry, "status": status, "reason": reason, "evidence": evidence,
                    "ts": datetime.now().astimezone().isoformat(),
                })
                if status in ("chat_started", "pending"):
                    applied_index.add(boss_key)
                    if key:
                        applied_index.add(key)
                if status == "chat_started":
                    applied_count += 1
                    print(f"  ✅ 接口确认已沟通 ({applied_count}/{target_count})")
                elif status == "pending":
                    pending_count += 1
                    print(f"  ⏳ 待核验，不重试、不计成功：{reason}")
                else:
                    print(f"  ❌ 沟通失败：{reason}")

                if clicked:
                    delay = random.uniform(*(FAST_INTERVAL if args.fast else CHAT_INTERVAL))
                    print(f"  ⏸️  沟通后休息 {delay:.1f} 秒")
                    time.sleep(delay)
                    check_page(entry)
                    if status == "chat_started" and applied_count % SUCCESS_REST_EVERY == 0:
                        delay = random.uniform(*(FAST_INTERVAL if args.fast else SUCCESS_REST))
                        print(f"  ☕ 每成功 {SUCCESS_REST_EVERY} 份额外休息 {delay:.1f} 秒")
                        time.sleep(delay)
                        check_page(entry)
                # 仅离开列表页时返回，避免每次沟通都清空无限滚动的位置。
                if "/web/geek/job" not in page.url:
                    page.get(search_url)
                    browse_sleep(4)

            except Exception as e:
                # SystemExit 不会落到这里，风控不能被普通失败处理吞掉。
                check_page(entry)
                err = str(e)[:120]
                print(f"  ❌ 失败: {err}")
                record(log, log_file, "failed", {**entry, "error": err})
                browse_sleep(2)

        if (dry_run_count if args.dry_run else applied_count) >= target_count:
            break
        # 已见卡片仍须继续滚动，不能因本屏全部去重而漏掉后续岗位。
        prev_count = len(page.eles(".job-card-wrap"))
        page.run_js("window.scrollTo(0, document.body.scrollHeight)")
        browse_sleep(3)
        check_page({"platform": "boss"})
        new_count = len(page.eles(".job-card-wrap"))
        if new_count <= prev_count:
            print("没有加载到新岗位，停止")
            break
        print(f"加载更多：{prev_count} → {new_count} 个")
        page_num += 1

    summary()


if __name__ == "__main__":
    main()
