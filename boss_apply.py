#!/usr/bin/env python3
"""Boss直聘自动投递脚本（DrissionPage版）"""

import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from apply_runtime import (CHAT_INTERVAL, SUCCESS_REST_EVERY, SUCCESS_REST,
                           VIEW_INTERVAL, VIEW_REST_EVERY, VIEW_REST)

from platform_runner import effective_decision
from jev_match import (
    MAX_CONSECUTIVE_FAILURES,
    JevError,
    build_state,
    clean_jd,
    format_result,
    latest_match,
    load_jev_key,
    match_job,
    match_title,
    title_protected,
    redact_resume,
    render_queue_md,
)
from shared import (
    BOSS_JOB_ID_PATTERN,
    classify_boss_chat_response,
    connect_chrome,
    cooldown_remaining,
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

# Boss 搜索筛选代码，来自 /wapi/zpgeek/pc/all/filter/conditions.json（2026-10-14 抓取）
JOB_TYPES = {"不限": "", "全职": "1901", "实习": "1902", "兼职": "1903"}
EXPERIENCES = {"在校生": "108", "应届生": "102", "经验不限": "101", "1年以内": "103",
               "1-3年": "104", "3-5年": "105", "5-10年": "106", "10年以上": "107"}
DEGREES = {"初中及以下": "209", "中专/中技": "208", "高中": "206", "大专": "202",
           "本科": "203", "硕士": "204", "博士": "205"}


def _codes(value, table, label):
    """逗号分隔的中文选项 → 逗号分隔代码；未知选项直接报错，不静默忽略。"""
    names = [v.strip() for v in (value or "").replace("，", ",").split(",") if v.strip()]
    unknown = [n for n in names if n not in table]
    if unknown:
        raise SystemExit(f"🛑 未知{label}: {unknown}，可选: {list(table)}")
    return ",".join(table[n] for n in names if table[n])


def build_search_url(job, city_code, job_type="实习", experience="", degree=""):
    params = [("city", city_code)]
    for key, value in (("jobType", _codes(job_type, JOB_TYPES, "求职类型")),
                       ("experience", _codes(experience, EXPERIENCES, "工作经验")),
                       ("degree", _codes(degree, DEGREES, "学历"))):
        if value:
            params.append((key, value))
    params.append(("query", job))
    return "https://www.zhipin.com/web/geek/jobs?" + urlencode(params, safe=",")


# 调试快模式不改变接口核验窗口；默认保留慢节奏。
CHAT_TARGET = "friend/add"
CHAT_RESPONSE_TIMEOUT = 8
FAST_INTERVAL = (3, 6)
FAST_VIEW_INTERVAL = (1, 3)
FAST_VIEW_REST = (3, 6)
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
                company: text('.company-name') || text('.boss-name'), salary: text('.job-salary'),
                tags: Array.from(c.querySelectorAll('.tag-list > *')).map(t => t.textContent.trim())};
    });
"""


def pick_boss_tab(tabs):
    """按现有顺序选第一个 Boss 岗位列表标签，不操作浏览器。"""
    return next((tab for tab in tabs if any(path in (tab.url or "") for path in (
        "zhipin.com/web/geek/jobs", "zhipin.com/web/geek/job"))), None)


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
        include_matcher=True,
    )
    city_code = CITY_CODES.get(args.city, "100010000")
    log_file = Path(__file__).parent / f"boss-{args.city}-log.json"
    queue_file = log_file.with_name(f"boss-{args.city}-队列.md")
    cache_file = log_file.with_name("jev-cache.json")
    cooldown_file = log_file.with_name("boss-cooldown.json")
    if not args.ignore_cooldown:
        remaining = cooldown_remaining(cooldown_file, datetime.now().astimezone())
        if remaining is not None:
            try:
                reason = json.loads(cooldown_file.read_text(encoding="utf-8")).get("reason", "风控冷却")
            except (OSError, ValueError, AttributeError):
                reason = "风控冷却"
            minutes = int(remaining.total_seconds() / 60) + 1
            print(f"🛑 Boss 风控冷却中，剩余约 {minutes} 分钟：{reason}；未连接浏览器。")
            raise SystemExit(2)

    log = load_log(log_file)
    applied_index = load_applied_index()
    applied_count = 0
    pending_count = 0
    skipped_count = 0
    dry_run_count = 0
    view_count = 0
    seen_job_ids = set()
    target_count = args.count
    rows = []
    consecutive_jev_failures = 0
    stop_reason = ""

    def summary():
        stats = {"city": args.city, "dry_run": args.dry_run,
                 "apply": sum(r.get("decision") == "apply" for r in rows),
                 "review": sum(r.get("decision") == "review" for r in rows),
                 "skip": sum(r.get("decision") == "skip" or r.get("status") == "already_contacted"
                             for r in rows), "applied": applied_count,
                 "pending": pending_count, "stop_reason": stop_reason}
        queue_file.write_text(render_queue_md(rows, stats), encoding="utf-8")
        print_summary("Boss直聘", applied_count, skipped_count, log_file, pending=pending_count)
        print(f"📋 推荐: {stats['apply']} 份  复核: {stats['review']} 份 | 队列: {queue_file}")
        print(f"👀 本轮打开详情: {view_count}/{args.max_views}")
        if stop_reason:
            print(f"停止原因：{stop_reason}")
        if args.dry_run:
            print(f"🧪 dry-run 已评分: {dry_run_count} 份（仅打印，未发起沟通）")

    def stop_blocked(reason, entry, evidence):
        nonlocal stop_reason
        stop_reason = reason
        entry.update(status="blocked", reason=reason)
        now = datetime.now().astimezone()
        try:
            cooldown_file.write_text(json.dumps({
                "until": (now + timedelta(hours=2)).isoformat(), "reason": reason,
                "page_url": evidence.get("page_url") or page.url,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            print("⚠️  冷却文件写入失败，本次仍立即终止。")
        record(log, log_file, "failed", {
            **entry, "status": "blocked", "reason": reason,
            "evidence": evidence, "ts": datetime.now().astimezone().isoformat(),
        })
        print(f"\n🛑🛑 检测到风控，立即终止整个脚本：{reason}\n")
        raise SystemExit(1)

    def check_page(entry):
        reason = page_block_reason(page)
        if reason:
            stop_blocked(reason, entry, {"packets": [], "page_url": page.url, "page_blocked": reason})

    def rest_after_view(entry):
        check_page(entry)  # 风控时立即退出，不等待休息。
        delay = random.uniform(*(FAST_VIEW_INTERVAL if args.fast else VIEW_INTERVAL))
        print(f"  👀 详情读取后休息 {delay:.1f} 秒（本轮 {view_count}/{args.max_views}）")
        time.sleep(delay)
        check_page(entry)
        if view_count % VIEW_REST_EVERY == 0:
            delay = random.uniform(*(FAST_VIEW_REST if args.fast else VIEW_REST))
            print(f"  ☕ 每浏览 {VIEW_REST_EVERY} 份额外休息 {delay:.1f} 秒")
            time.sleep(delay)
            check_page(entry)

    try:
        jev_key, resume_text, profile = "", "", ""
        if args.matcher == "jev":
            jev_key = load_jev_key()
            if not jev_key:
                stop_reason = "缺少 JEV_API_KEY"
                print("🛑🛑 Jev 模式缺少 JEV_API_KEY：请配置环境变量或 ~/.config/jobhunter/jev.env；未启动浏览器，不会降级。")
                raise SystemExit(2)
            try:
                resume_path = Path(cfg["resume_path"]).expanduser()
                if not resume_path.is_absolute():
                    resume_path = Path(__file__).parent / resume_path
                resume_text = resume_path.read_text(encoding="utf-8")
                if not resume_text.strip():
                    raise ValueError("空简历")
            except (OSError, UnicodeError, ValueError):
                stop_reason = "简历不可读或为空"
                print("🛑 请将 config.resume_path 指向非空 UTF-8 文本简历（.md / .txt；PDF 请先转文本）。")
                raise SystemExit(2)
            resume_text = redact_resume(resume_text, cfg["resume_redact"])
            profile = redact_resume(cfg["job_profile"], cfg["resume_redact"])

        print(f"🎯 Boss直聘 | 岗位: {args.job} | 城市: {args.city} | 目标: {target_count} 份 | 匹配: {args.matcher}")
        if args.dry_run:
            print("🧪 dry-run：只浏览和打分，--count 为评分数量上限")
        if args.fast:
            print("⚠️  --fast 调试模式：详情间隔 1–3 秒，沟通间隔及额外休息 3–6 秒")
        print("🔗 连接 Chrome（端口 9222）...")
        browser = connect_chrome()

        search_url = build_search_url(args.job, city_code, args.job_type, args.experience, args.degree)
        print(f"📋 打开搜索页：{search_url}")
        page = pick_boss_tab(browser.get_tabs())
        if page is None:
            page = browser.new_tab(search_url)
        else:
            page.get(search_url)
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
                if view_count >= args.max_views:
                    stop_reason = f"已达 --max-views 上限（{args.max_views}），停止扫描"
                    break
                # --fill-queue 保留 Jev 投满后评完当前批次的旧行为。
                if ((args.dry_run and dry_run_count >= target_count)
                        or (not args.dry_run and applied_count >= target_count
                            and (not args.fill_queue or args.matcher == "keyword"))):
                    break
                job_id, title = card["job_id"], card["title"]
                company, salary = card["company"], card["salary"]
                entry = {"platform": "boss", "job_id": job_id, "company": company,
                         "job": title, "salary": salary,
                         "url": f"https://www.zhipin.com/job_detail/{job_id}.html" if job_id else ""}
                seen_key = job_id or f"无ID:{company}|{title}"
                if seen_key in seen_job_ids:
                    continue
                seen_job_ids.add(seen_key)
                rows.append(entry)
                key = dedup_key(company, title)
                boss_key = f"boss:{job_id}"

                skip_reason = ""
                if not job_id:
                    skip_reason = "无职位ID"
                elif is_blacklisted(company, cfg):
                    skip_reason = "公司黑名单"
                elif boss_key in applied_index or (key and key in applied_index):
                    skip_reason = "已投过或待核验（跨运行去重）"
                elif args.matcher == "jev":
                    for kw in cfg["exclude_keywords"]:
                        if kw and kw.lower() in title.lower():
                            skip_reason = f"标题包含排除词: {kw}"
                            break
                if skip_reason:
                    skipped_count += 1
                    entry.update(decision="skip", reason=skip_reason)
                    record(log, log_file, "skipped", {**entry, "reason": skip_reason})
                    print(f"⏭️  [{skip_reason}] {company} | {title} | ID={job_id}")
                    continue

                try:
                    if args.matcher == "jev":
                        cached = None if args.refresh else latest_match(job_id, cache_file)
                        if cached and cached["decision"]["decision"] in ("skip", "review"):
                            decision = cached["decision"]
                            entry.update(jev=cached, decision=decision["decision"],
                                         reason=f"[缓存] {decision['reason']}")
                            if entry["decision"] == "skip":
                                skipped_count += 1
                            if args.dry_run:
                                dry_run_count += 1
                            record(log, log_file, "skipped" if entry["decision"] == "skip" else "review", {**entry})
                            print(f"[{entry['decision']}] {company} | {title} → {entry['reason']}")
                            continue
                        title_state = (f"【岗位卡片】{title}｜{company}｜{salary}｜标签: {card.get('tags', [])}\n"
                                       f"【求职偏好】{profile}")
                        title_skip = False
                        try:
                            if not title_protected(title):
                                title_skip, title_reason = match_title(
                                    job_id, title_state, jev_key, cache_file, refresh=args.refresh)
                        except JevError:
                            title_skip = False
                            print(f"  标题粗筛失败，继续读取 JD：{company} | {title}")
                        if title_skip:
                            skipped_count += 1
                            if args.dry_run:
                                dry_run_count += 1
                            entry.update(decision="skip", reason=title_reason)
                            record(log, log_file, "skipped", {**entry})
                            print(f"⏭️  [{title_reason}] {company} | {title}")
                            continue
                    check_page(entry)
                    # 按 href 中的完整职位 ID 实时定位卡片，绝不按同名标题匹配。
                    link = page.ele(
                        f'css:.job-card-wrap a[href*="job_detail/{job_id}.html"]', timeout=2
                    )
                    if not link:
                        raise RuntimeError(f"未找到职位卡片: {job_id}")
                    # JS 点击不会切换右侧详情，必须原生点击 .job-card-box
                    box = link.parent("css:.job-card-wrap").ele("css:.job-card-box")
                    view_count += 1
                    try:
                        box.click()
                        browse_sleep(2)
                        check_page(entry)
                        # 详情标题必须对上当前卡片，否则后面的"立即沟通"会发给别的岗位
                        if not wait_detail_title(page, title):
                            skipped_count += 1
                            entry.update(decision="skip", reason="详情未切换到该岗位")
                            record(log, log_file, "skipped", {**entry, "reason": "详情未切换到该岗位"})
                            print(f"⏭️  [详情未切换] {company} | {title}")
                            continue
                        desc_el = page.ele(".job-detail-body") or page.ele(".job-sec-text")
                        if not desc_el:
                            raise RuntimeError("未找到岗位 JD，未发起沟通")
                        jd_text = clean_jd(desc_el.text)
                    finally:
                        if entry.get("status") != "blocked":
                            rest_after_view(entry)
                    if args.dry_run:
                        dry_run_count += 1
                        entry["keyword_score"] = score_jd(title, jd_text, cfg)[0]
                    if args.matcher == "jev":
                        try:
                            result = match_job(job_id, jd_text,
                                               build_state(title, company, jd_text, resume_text, profile),
                                               jev_key, cache_file, refresh=args.refresh)
                        except JevError:
                            consecutive_jev_failures += 1
                            entry.update(decision="review", reason="Jev 调用失败",
                                         jev={"error": "Jev 调用失败"})
                            record(log, log_file, "review", {**entry})
                            print(f"[review] hard=— fit=— action=复核 p=— | {company} | {title} → Jev 调用失败")
                            if consecutive_jev_failures >= MAX_CONSECUTIVE_FAILURES:
                                stop_reason = "Jev 连续调用失败 3 次"
                                print(f"🛑 {stop_reason}，停止脚本。")
                                raise SystemExit(1)
                            continue
                        consecutive_jev_failures = 0
                        decision = effective_decision(result, args.match_policy)
                        entry.update(jev=result, decision=decision["decision"], reason=decision["reason"])
                        metrics = format_result(decision)
                        print(f"[{decision['decision']}] {metrics} | {company} | {title} → {decision['reason']}")
                        if entry["decision"] == "review":
                            record(log, log_file, "review", {**entry})
                            continue
                        if entry["decision"] == "skip":
                            skipped_count += 1
                            entry["reason"] = f"{decision['reason']}（{metrics}）"
                            record(log, log_file, "skipped", {**entry})
                            continue
                    else:
                        score, reason = score_jd(title, jd_text, cfg)
                        entry.update(score=score, decision="apply" if score >= args.min_score else "skip",
                                     reason=reason)
                        print(f"[{score:3d}分] {company} | {title} | {salary} | ID={job_id} → {reason}")
                        # keyword dry-run 保持 P0：评分不写 skipped。
                        if not args.dry_run and score < args.min_score:
                            skipped_count += 1
                            record(log, log_file, "skipped", {**entry, "reason": reason})
                            continue

                    if args.dry_run or applied_count >= target_count:
                        if entry["decision"] == "apply":
                            entry["reason"] += "；dry-run 未沟通" if args.dry_run else "；已达 count 上限，未沟通"
                        continue

                    btn_disabled = page.run_js(
                        'var b=document.querySelector(".op-btn-chat"); return b ? b.classList.contains("is-disabled") : false;'
                    )
                    if btn_disabled:
                        skipped_count += 1
                        entry.update(status="already_contacted", reason="已沟通过")
                        record(log, log_file, "skipped", {**entry, "reason": "已沟通过"})
                        continue

                    status, reason, evidence, clicked = start_chat(page, job_id)
                    entry.update(status=status, reason=reason, attempted=clicked)
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

                    if applied_count >= target_count and (not args.fill_queue or args.matcher == "keyword"):
                        stop_reason = f"已达 --count 上限（{target_count}），停止扫描"
                        break
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

            if view_count >= args.max_views:
                stop_reason = stop_reason or f"已达 --max-views 上限（{args.max_views}），停止扫描"
                break
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

    finally:
        summary()


if __name__ == "__main__":
    main()
