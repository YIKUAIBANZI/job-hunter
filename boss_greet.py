#!/usr/bin/env python3
"""Boss直聘个性化招呼语（可选）：对已沟通、HR 尚未回复的会话补发一条带岗位名的招呼语。

投递脚本只发平台默认招呼语；本脚本单独运行，不跑就不生效。
默认只预览，加 --send 才真正发送。只读日志里 applied 的岗位，打开会话后
核对公司 + 岗位名一致才发；HR 已回复、已补发过、页面异常一律跳过。
"""

import argparse
import json
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from boss_apply import page_block_reason
from shared import connect_chrome, cooldown_remaining, load_config, load_log

CHAT_URL = "https://www.zhipin.com/web/geek/chat"
SEND_INTERVAL = (20, 60)
SEND_CONFIRM_TIMEOUT = 6
DEFAULT_TEMPLATE = "您好，关于贵司「{job}」岗位：{greeting}"


# ---------- 纯函数 ----------


def build_greeting(cfg: dict, entry: dict) -> str:
    """config.greeting_template 可用 {job} {company} {greeting}；greeting 去掉开头的“您好”避免重复。"""
    greeting = (cfg.get("greeting") or "").strip()
    for prefix in ("您好，", "您好！", "您好,", "您好!", "您好"):
        if greeting.startswith(prefix):
            greeting = greeting[len(prefix):]
            break
    template = cfg.get("greeting_template") or DEFAULT_TEMPLATE
    job = re.sub(r"\s*[（(][A-Za-z]*\d+[)）]\s*$", "", entry.get("job", ""))  # 去掉岗位编号
    return template.format(job=job, company=entry.get("company", ""),
                           greeting=greeting).strip()


def pick_targets(log: dict, greeted: dict, now: datetime, since_days: float) -> list[dict]:
    """applied 中 since_days 内、带 job_id、尚未处理过的岗位，按时间先后。"""
    since = now - timedelta(days=since_days)
    targets = []
    for entry in log.get("applied", []):
        job_id = entry.get("job_id")
        if not job_id or job_id in greeted or not entry.get("company") or not entry.get("job"):
            continue
        try:
            ts = datetime.fromisoformat(entry["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if ts >= since:
            targets.append(entry)
    return sorted(targets, key=lambda e: e["ts"])


def load_greeted(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_greeted(path: Path, greeted: dict):
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(greeted, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


# ---------- 页面操作 ----------


def open_conversation(tab, company: str, job: str) -> str:
    """在会话列表里找公司名一致的会话，逐个打开核对右侧岗位名；返回 ok / not_found。"""
    tried = 0
    for _ in range(4):
        hits = [e for e in tab.eles(".friend-content")
                if len(spans := e.eles("css:.name-box > span")) > 1 and spans[1].text.strip() == company]
        for item in hits[tried:]:
            tried += 1
            item.scroll.to_see()
            item.click()  # 原生点击，和人工一致
            time.sleep(random.uniform(1.5, 3))
            name = tab.run_js("const e=document.querySelector('.chat-position-content .position-name');"
                              "return e ? e.innerText.trim() : '';")
            if name == job:
                return "ok"
        # 列表懒加载：滚到底再找
        tab.run_js("""
            const li = document.querySelector('.friend-content');
            let box = li && li.parentElement;
            while (box && box.scrollHeight <= box.clientHeight) box = box.parentElement;
            if (box) box.scrollTop = box.scrollHeight;
        """)
        time.sleep(random.uniform(1.5, 3))
    return "not_found"


def hr_replied(tab) -> bool:
    return bool(tab.run_js("return document.querySelectorAll('.message-item.item-friend').length > 0;"))


def send_message(tab, text: str) -> tuple[str, str]:
    """输入并点发送；以对话区出现自己的新消息为成功证据。返回 (sent|pending|failed, reason)。"""
    before = tab.run_js("return document.querySelectorAll('.message-item.item-myself').length;")
    box = tab.ele("#chat-input", timeout=3)
    if not box:
        return "failed", "未找到输入框"
    box.click()
    box.input(text)
    time.sleep(random.uniform(0.8, 1.6))
    btn = tab.ele("css:button.btn-send", timeout=2)
    if not btn or "disabled" in (btn.attr("class") or ""):
        return "failed", "发送按钮不可用"
    btn.click()
    deadline = time.monotonic() + SEND_CONFIRM_TIMEOUT
    probe = text[:12]
    while time.monotonic() < deadline:
        ok = tab.run_js("""
            const mine = [...document.querySelectorAll('.message-item.item-myself')];
            return mine.length > arguments[0] && mine.some(e => (e.innerText || '').includes(arguments[1]));
        """, before, probe)
        if ok:
            return "sent", "对话区出现新消息"
        time.sleep(0.5)
    return "pending", "已点击发送，未见新消息"


# ---------- 主流程 ----------


def main(argv=None):
    p = argparse.ArgumentParser(description="Boss 已沟通岗位补发个性化招呼语（默认只预览）")
    p.add_argument("--city", default="深圳")
    p.add_argument("--count", type=int, default=10, help="本轮最多发送条数（默认 10）")
    p.add_argument("--since-days", type=float, default=1, help="只处理多少天内的投递（默认 1）")
    p.add_argument("--send", action="store_true", help="真正发送；不加只打印预览")
    p.add_argument("--job-id", help="只处理这个职位 ID（首次校准用）")
    p.add_argument("--ignore-cooldown", action="store_true")
    args = p.parse_args(argv)

    root = Path(__file__).parent
    cfg = load_config()
    log = load_log(root / f"boss-{args.city}-log.json")
    greeted_file = root / "boss-greet.json"
    greeted = load_greeted(greeted_file)
    now = datetime.now().astimezone()
    targets = pick_targets(log, greeted, now, args.since_days)
    if args.job_id:
        targets = [e for e in targets if e["job_id"] == args.job_id]
    targets = targets[:args.count]

    print(f"💬 待补发 {len(targets)} 条（{args.since_days} 天内、未处理过）{'' if args.send else '｜预览模式'}")
    for e in targets:
        print(f"  - {e['company']} | {e['job']}\n    → {build_greeting(cfg, e)}")
    if not args.send or not targets:
        return 0

    cooldown_file = root / "boss-cooldown.json"
    if not args.ignore_cooldown:
        left = cooldown_remaining(cooldown_file, now)
        if left:
            print(f"🧊 风控冷却中，还剩 {left}，退出")
            return 2

    tab = connect_chrome().new_tab(CHAT_URL)
    time.sleep(random.uniform(4, 7))
    counts = {"sent": 0, "pending": 0, "skipped": 0, "failed": 0}
    try:
        for n, e in enumerate(targets):
            block = page_block_reason(tab)
            if block:
                print(f"🛑 风控：{block}，停止")
                return 1
            state = open_conversation(tab, e["company"], e["job"])
            if state != "ok":
                status, reason = "skipped", "会话列表里没找到公司+岗位一致的会话"
            elif hr_replied(tab):
                status, reason = "skipped", "HR 已回复，留给你人工跟进"
            else:
                status, reason = send_message(tab, build_greeting(cfg, e))
            # skipped（未找到）不落盘，下次还能重试；其余都记下，避免重复发送
            if not (status == "skipped" and state != "ok"):
                greeted[e["job_id"]] = {"status": status, "reason": reason,
                                        "ts": datetime.now().astimezone().isoformat()}
                save_greeted(greeted_file, greeted)
            counts[status] += 1
            print(f"[{status}] {e['company']} | {e['job']} → {reason}")
            if status in ("sent", "pending") and n < len(targets) - 1:
                delay = random.uniform(*SEND_INTERVAL)
                print(f"  ⏳ 休息 {delay:.0f} 秒")
                time.sleep(delay)
    finally:
        print(f"\n📊 发送 {counts['sent']}｜待核验 {counts['pending']}｜跳过 {counts['skipped']}｜失败 {counts['failed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
