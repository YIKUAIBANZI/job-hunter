#!/usr/bin/env python3
"""只读回复日报：只读取列表，不打开会话、不输入、不发送。

猎聘会话列表需点击“我的沟通”入口展开抽屉；只点入口，不点任何会话。
"""

import argparse
import html
import random
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

REPORT_FILE = '回复日报.md'
BOSS_URL = 'https://www.zhipin.com/web/geek/chat'
# c.liepin.com 首页的入口点不开抽屉，www 域名的列表/详情页可以（实机确认）
LIEPIN_URL = 'https://www.liepin.com/zhaopin/'


# ---------- 纯函数 ----------

def classify_boss_item(item) -> str:
    """只有状态元素缺失才推断对方回复；空白或异常状态保持未知。"""
    if not isinstance(item, dict):
        return 'unknown'
    status = item.get('status')
    if status is None:
        # 平台系统消息（如“您的附件简历…已发送给Boss”）也没有状态元素，不算 HR 回复
        # 猎聘同理：“您可以修改打招呼语”“为您生成了合适的打招呼语”是平台提示
        if re.search(r'附件简历.*已发送|已发送给Boss|修改打招呼语|生成了合适的打招呼语',
                     str(item.get('last_msg') or '')):
            return 'delivered'
        return 'replied'
    if not isinstance(status, str):
        return 'unknown'
    if '已读' in status:
        return 'read'
    # 猎聘的“[未读]”指对方还没读我的消息，等同送达
    if '送达' in status or '未读' in status:
        return 'delivered'
    return 'unknown'


def _text(value) -> str:
    return str(value) if value is not None else ''


def _company(value) -> str:
    return re.sub(r'\s+', '', _text(value)).lower()


def match_applied(items, log) -> list[dict]:
    """只按公司关联 applied；多岗全部保留，不能据此确认回复针对哪个岗位。"""
    entries = log.get('applied', [])
    result = []
    for item in items:
        company = _company(item.get('company'))
        prefix = company[:-3] if company.endswith('...') else None
        matches = [dict(entry) for entry in entries if isinstance(entry, dict)
                   and _company(entry.get('company'))
                   and ((bool(prefix) and _company(entry.get('company')).startswith(prefix))
                        if prefix is not None else
                        (bool(company) and _company(entry.get('company')) == company))]
        row = {**item, 'source': '本工具投递' if matches else '非本工具投递',
               'applied_matches': matches,
               'job': ' / '.join(dict.fromkeys(_text(e.get('job')) for e in matches
                                               if e.get('job'))),
               'applied_at': ' / '.join(dict.fromkeys(_text(e.get('ts')) for e in matches
                                                      if e.get('ts')))}
        result.append(row)
    return result


def dedup_boss_items(items) -> list[dict]:
    """按公司、姓名、列表时间去重；重复快照用最新内容，保留初见顺序。"""
    unique = {}
    for item in items:
        key = tuple(_text(item.get(field)) for field in ('company', 'name', 'time'))
        unique[key] = dict(item)
    return list(unique.values())


def _cell(value) -> str:
    # 先转义 HTML 和 Markdown，再折叠换行，防止消息伪造表格行或链接。
    text = html.escape(_text(value), quote=False)
    text = text.replace('\\', '\\\\')
    text = re.sub(r'([`*_\[\]])', r'\\\1', text)
    return text.replace('|', '&#124;').replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ')


def _counts(items):
    return Counter(classify_boss_item(item) for item in items)


def _summary(label, items):
    counts = _counts(items)
    return (f'{label}：会话 {len(items)}｜HR 已回复 {counts["replied"]}｜'
            f'已读未回 {counts["read"]}｜仅送达 {counts["delivered"]}')


def render_report(boss_items, liepin_items, now) -> str:
    """liepin_items 为 None 表示未读取（--no-liepin）。"""
    generated = now.isoformat(sep=' ', timespec='seconds') if isinstance(now, datetime) else _text(now)
    liepin = ('猎聘：未读取（--no-liepin）' if liepin_items is None
              else _summary('猎聘', liepin_items))
    lines = ['# 回复日报', '', f'生成时间：{_cell(generated)}', '',
             _summary('Boss', boss_items), liepin, '',
             '仅汇总当前加载到的会话，Boss 最多滚动 5 次；不代表全部历史会话。',
             '公司匹配仅用于关联投递记录，同公司多个岗位全部列出，不能确认回复对应的具体岗位。',
             '岗位标为“非本工具投递”表示未匹配所选城市的 applied 日志。', '']
    sections = [('Boss', boss_items)] + ([('猎聘', liepin_items)] if liepin_items is not None else [])
    for platform, items in sections:
        lines += _render_platform(platform, items)
    return '\n'.join(lines)


def _render_platform(platform, items) -> list[str]:
    counts = _counts(items)
    lines = []
    if counts['unknown']:
        lines += [f'{platform} 状态未知：{counts["unknown"]}', '']
    for state, heading, headers, fields in (
        ('replied', 'HR 已回复（优先处理）', ['公司', '岗位', '招聘者', '时间', '最后一条消息'],
         ('company', 'job', 'name', 'time', 'last_msg')),
        ('read', '已读未回', ['公司', '岗位', '时间'], ('company', 'job', 'time')),
    ):
        lines += [f'## {platform}｜{heading}', '']
        rows = [item for item in items if classify_boss_item(item) == state]
        if not rows:
            lines += ['暂无', '']
            continue
        lines += ['| ' + ' | '.join(headers) + ' |', '|' + '---|' * len(headers)]
        for row in rows:
            values = []
            for field in fields:
                value = row.get(field)
                if field == 'job':
                    value = value or row.get('source') or '—'
                if field == 'last_msg':
                    value = _text(value)
                    value = value[:59] + '…' if len(value) > 60 else value
                values.append(_cell(value) or '—')
            lines.append('| ' + ' | '.join(values) + ' |')
        lines.append('')
    lines += [f'{platform} 仅送达：{counts["delivered"]} 个会话', '']
    return lines


# ---------- 页面读取（仅 DOM 读取与列表滚动） ----------

BOSS_ITEMS_JS = """
    return [...document.querySelectorAll('.friend-content')].map(item => {
        const text = selector => {
            const e = item.querySelector(selector);
            return e ? (e.innerText || '').trim() : '';
        };
        const spans = [...item.querySelectorAll('.name-box > span')];
        const span = i => spans[i] ? (spans[i].innerText || '').trim() : '';
        const status = item.querySelector('.message-status');
        return {company: span(1), name: span(0), title: span(3),
                time: text('.time'), last_msg: text('.last-msg-text'),
                status: status ? (status.innerText || '').trim() : null};
    });
"""

BOSS_SCROLL_JS = """
    const item = document.querySelector('.friend-content');
    let box = item && item.parentElement;
    while (box) {
        const overflow = getComputedStyle(box).overflowY;
        if (box.scrollHeight > box.clientHeight && /auto|scroll|overlay/.test(overflow)) {
            box.scrollTop = box.scrollHeight;
            return true;
        }
        box = box.parentElement;
    }
    return false;
"""

LIEPIN_ITEMS_JS = """
    return [...document.querySelectorAll('.im-ui-contact-item')].map(item => {
        const text = selector => {
            const e = item.querySelector(selector);
            return e ? (e.innerText || '').trim() : '';
        };
        const status = item.querySelector('.im-ui-message-read-status');
        // 副标题形如“HRBP·深圳某某科技有限公司”，公司取第一个“·”之后
        const sub = text('.im-ui-contact-title-sub');
        const i = sub.indexOf('·');
        let last = text('.im-ui-last-message');
        if (status) last = last.replace((status.innerText || '').trim(), '').trim();
        return {name: text('.im-ui-contact-title-name'),
                title: i >= 0 ? sub.slice(0, i) : '', company: i >= 0 ? sub.slice(i + 1) : sub,
                time: text('.contact-time'), last_msg: last,
                status: status ? (status.innerText || '').trim() : null};
    });
"""


def read_boss_items(tab, check_block) -> list[dict]:
    """每个快照读取前后检查风控，初始快照后最多滚动五次。"""
    items = []
    for turn in range(6):
        check_block(tab)
        snapshot = tab.run_js(BOSS_ITEMS_JS)
        check_block(tab)
        if not isinstance(snapshot, list) or any(not isinstance(row, dict) for row in snapshot):
            raise ValueError('Boss 会话列表读取结果异常')
        items = dedup_boss_items([*items, *snapshot])
        if turn == 5:
            break
        check_block(tab)
        scrolled = tab.run_js(BOSS_SCROLL_JS)
        check_block(tab)
        if not scrolled:
            break
        time.sleep(random.uniform(1.5, 3))
    return items


def read_liepin_items(tab, check_block) -> list[dict]:
    """原生点击“我的沟通”入口展开抽屉后读取列表；不点击任何会话。"""
    check_block(tab)
    entry = tab.ele('css:.im-ui-basic-entry', timeout=5)
    if not entry:
        raise ValueError('猎聘“我的沟通”入口未找到')
    entry.click()
    time.sleep(random.uniform(3, 4))
    check_block(tab)
    items = tab.run_js(LIEPIN_ITEMS_JS)
    check_block(tab)
    if not isinstance(items, list) or any(not isinstance(row, dict) for row in items):
        raise ValueError('猎聘会话列表读取结果异常')
    return dedup_boss_items(items)


class PageBlocked(RuntimeError):
    """风控已经写冷却；主流程不再写日报。"""


def main(argv=None):
    parser = argparse.ArgumentParser(description='只读汇总 Boss 与猎聘会话回复，不打开任何会话')
    parser.add_argument('--city', default='深圳', help='匹配 boss-<城市>-log.json（默认深圳，不过滤会话）')
    parser.add_argument('--no-liepin', action='store_true', help='不打开猎聘标签、不读取猎聘会话')
    args = parser.parse_args(argv)

    # 延迟导入，让纯函数及 --help 不依赖浏览器连接，也不读取个人配置。
    import shared
    from apply_runtime import write_cooldown
    from boss_apply import page_block_reason
    from shared import connect_chrome, cooldown_remaining, load_log, log_path, cooldown_path, report_path

    root = shared.PROJECT_ROOT
    cooldown_file = cooldown_path(root, 'boss')
    left = cooldown_remaining(cooldown_file, datetime.now().astimezone())
    if left:
        print(f'风控冷却中，剩余 {left}；未连接浏览器，日报未更新')
        return 1

    def check_block(tab):
        reason = page_block_reason(tab)
        if reason:
            try:
                write_cooldown(cooldown_file, reason)
            except OSError as exc:
                print(f'冷却文件写入失败：{exc}')
            raise PageBlocked(reason)

    tabs = []
    try:
        log = load_log(log_path(root, 'boss', args.city))
        browser = connect_chrome()

        def open_tab(url):
            tab = browser.new_tab()
            tabs.append(tab)  # 导航或等待失败也要关闭自己的标签。
            tab.get(url)
            check_block(tab)
            time.sleep(random.uniform(5, 7))
            check_block(tab)
            return tab

        boss_tab = open_tab(BOSS_URL)
        boss_items = match_applied(read_boss_items(boss_tab, check_block), log)
        liepin_items = None if args.no_liepin else match_applied(
            read_liepin_items(open_tab(LIEPIN_URL), check_block),
            load_log(log_path(root, 'liepin', args.city)))
        # 猎聘读取期间 Boss 也可能出现风控，写报告前再次检查所有自建标签。
        for tab in tabs:
            check_block(tab)
        report = render_report(boss_items, liepin_items, datetime.now().astimezone())
    except PageBlocked as exc:
        print(f'风控：{exc}；停止，日报未更新')
        return 1
    except Exception as exc:
        print(f'读取失败：{exc}；日报未更新')
        return 1
    finally:
        for tab in reversed(tabs):
            try:
                tab.close()
            except Exception as exc:
                print(f'自建标签关闭失败：{exc}')

    try:
        path = report_path(root, REPORT_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding='utf-8')
    except OSError as exc:
        print(f'日报写入失败：{exc}')
        return 1
    liepin = '猎聘未读取' if liepin_items is None else _summary('猎聘', liepin_items)
    print(f'{REPORT_FILE} 已更新｜{_summary("Boss", boss_items)}｜{liepin}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
