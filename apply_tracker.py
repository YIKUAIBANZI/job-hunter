#!/usr/bin/env python3
"""离线维护官网网申进度；只读缓存，绝不打开网页或填写表单。"""
import argparse
import html
import json
import math
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from official_apply import collect_companies, collect_manual_forms, is_aggregator, normalize_company

from shared import EMAIL_PATTERN, cache_path, extract_emails, report_path

ROOT = Path(__file__).resolve().parent
STATES = ('未投', '已投', '笔试/测评', '面试', 'Offer', '结束')
HEADERS = ('公司', '入口', '核验', '截止日期', '状态', '备注', '来源岗位')
EXPIRED = '(缓存已过期)'


def url_key(url) -> str:
    """URL 为主键：协议/域名小写，去查询、片段及末尾斜杠。"""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() == 'mailto':
        return 'mailto:' + parts.path.lower()
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path.rstrip('/'), '', ''))


def _valid_url(url):
    if not isinstance(url, str) or re.search(r'[\s<>\\\x00-\x1f\x7f]', url):
        return False
    try:
        parts = urlsplit(url)
        parts.port
        if parts.scheme == 'mailto':
            return not (parts.netloc or parts.query or parts.fragment) and bool(EMAIL_PATTERN.fullmatch(parts.path))
        return parts.scheme in ('http', 'https') and bool(parts.hostname) and not (
            parts.username or parts.password)
    except ValueError:
        return False


def _cell(value):
    # 用实体编码表格分隔符及 Markdown 标记，解析后可恢复用户原文。
    text = html.escape(str(value), quote=False)
    for char in '\\`[]!*_|':
        text = text.replace(char, f'&#{ord(char)};')
    return text.replace('\r', '&#13;').replace('\n', '&#10;')


def _decode(value):
    return html.unescape(re.sub(r'\\([\\`*_{\[\]()#+.!|>}-])', r'\1', value))


def parse_tracker(md) -> dict:
    """仅解析主表，忽略统计/提醒；也支持用户手写的反斜杠转义。"""
    previous = {}
    in_table = False
    for line in md.splitlines():
        if not line.strip().startswith('|'):
            in_table = False
            continue
        cells = [value.strip() for value in re.split(r'(?<!\\)\|', line.strip())[1:-1]]
        if tuple(cells) == HEADERS:
            in_table = True
            continue
        if not in_table or len(cells) != len(HEADERS) or all(
                re.fullmatch(r':?-+:?', cell) for cell in cells):
            continue
        link = re.fullmatch(r'\[(.*?)\]\((?:<([^<>]+)>|([^\s]+))\)', cells[1])
        if not link:
            continue
        url = html.unescape(link.group(2) or link.group(3))
        if not _valid_url(url):
            continue
        company, _, verification, deadline, status, note, jobs = map(_decode, cells)
        key = url_key(url)
        previous[key] = {'key': key, 'company': company, 'url': url,
                         'title': _decode(link.group(1)),
                         'verification': verification.replace(EXPIRED, '').strip(),
                         'deadline': deadline, 'status': status, 'note': note,
                         'jobs': jobs, 'expired': EXPIRED in verification}
    return previous


def _probability(candidate):
    value = candidate.get('official', 0)
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else 0


def build_rows(cache, previous, manual, companies=None, manual_forms=None) -> list[dict]:
    """从缓存选入口并回填手工字段；参数不被修改，输出按选取顺序排列。"""
    rows = {}

    def add(row):
        key = url_key(row['url'])
        row = {'key': key, 'deadline': '', 'status': '未投', 'note': '',
               'jobs': '', 'expired': False, **row}
        old = previous.get(key, {})
        for field in ('deadline', 'status', 'note'):
            if field in old:
                row[field] = old[field]
        # 手动来源持续保留，缓存刷新不会改变这一来源。
        if old.get('verification') == '手动':
            row['verification'] = '手动'
        rows[key] = row

    # 同一归一化公司的多份数据合并，再施加最多两个唯一入口的限制。
    cached_companies = {}
    for entry in cache.values():
        if not isinstance(entry, dict) or not isinstance(entry.get('result'), dict):
            continue
        result = entry['result']
        company = result.get('company', '')
        if not isinstance(company, str) or not company.strip():
            continue
        group = cached_companies.setdefault(normalize_company(company), [])
        jobs = '、'.join(str(job) for job in result.get('jobs', []))
        for candidate in result.get('candidates', []):
            # 旧缓存可能含后来才排除的聚合站，这里再过滤一次
            if (isinstance(candidate, dict) and candidate.get('status') in ('已核验', '待核验')
                    and _valid_url(candidate.get('url')) and not is_aggregator(candidate['url'])):
                group.append((candidate, company, jobs))
    for group in cached_companies.values():
        seen = set()
        for candidate, company, jobs in sorted(group, key=lambda item: (
                item[0]['status'] != '已核验', -_probability(item[0]))):
            key = url_key(candidate['url'])
            if key in seen:
                continue
            seen.add(key)
            if key not in rows:
                add({'company': company, 'url': candidate['url'],
                     'title': candidate.get('title') or company,
                     'verification': candidate['status'], 'jobs': jobs})
            if len(seen) == 2:
                break
    for company in companies if companies is not None else collect_companies(ROOT):
        for email in company.get('emails', []):
            # 只使用日志中的合法投递地址，入口键按小写邮箱保留手工字段。
            if extract_emails(email) == [email.lower()]:
                add({'company': company['company'], 'url': 'mailto:' + email.lower(),
                     'title': email.lower(), 'verification': 'JD邮箱',
                     'jobs': '、'.join(company.get('jobs', []))})
    for form in manual_forms if manual_forms is not None else collect_manual_forms(ROOT):
        if _valid_url(form.get('url')):
            add({'company': form.get('company', ''), 'url': form['url'],
                 'title': form.get('job') or '岗位表单', 'verification': '平台表单',
                 'jobs': form.get('job', '')})
    for item in manual or []:
        # 手动输入为 {company, url} 列表；CLI 校验 URL，纯函数跳过无效入口。
        if _valid_url(item.get('url')):
            add({'company': item['company'], 'url': item['url'],
                 'title': item.get('title') or item['company'], 'verification': '手动'})
    for key, old in previous.items():
        if key not in rows and (old.get('verification') == '手动' or
                old.get('status', '未投') != '未投' or old.get('note')):
            rows[key] = {**old, 'expired': old.get('verification') != '手动'}
    return list(rows.values())


def _due_rows(rows, now):
    today = now.date() if isinstance(now, datetime) else now
    due = []
    for row in rows:
        deadline = row.get('deadline', '')
        if row.get('status') == '结束' or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', deadline):
            continue
        try:
            end = date.fromisoformat(deadline)
        except ValueError:
            continue
        if 0 <= (end - today).days <= 7:
            due.append(row)
    return sorted(due, key=lambda row: row['deadline'])


def _link(row):
    url = html.escape(row['url'], quote=False).replace('|', '&#124;')
    return f'[{_cell(row["title"][:30])}](<{url}>)'


def render_tracker(rows, now) -> str:
    """渲染统计与主表；日期和当前时间由调用者传入。"""
    counts = {state: 0 for state in (*STATES, '其他')}
    for row in rows:
        state = row.get('status', '未投')
        counts[state if state in STATES else '其他'] += 1
    due = _due_rows(rows, now)
    lines = ['# 网申进度', '', f'生成时间：{now.isoformat(sep=" ", timespec="seconds") if isinstance(now, datetime) else now.isoformat()}',
             '统计：入口数 ' + str(len(rows)) + '｜' + '｜'.join(
                 f'{state} {count}' for state, count in counts.items()),
             f'即将截止（7 天内）：{len(due)} 个', '', '## 即将截止（7 天内）', '']
    lines += [f'- {row["deadline"]}｜{_cell(row["company"])}｜{_link(row)}' for row in due] or ['暂无。']
    lines += ['', '## 入口进度', '', '| ' + ' | '.join(HEADERS) + ' |',
              '|---|---|---|---|---|---|---|']
    for row in rows:
        verification = row['verification'] + (f' {EXPIRED}' if row.get('expired') else '')
        cells = [_cell(row['company']), _link(row),
                 _cell(verification), _cell(row.get('deadline', '')),
                 _cell(row.get('status', '未投')), _cell(row.get('note', '')),
                 _cell(row.get('jobs', ''))]
        lines.append('| ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线生成网申进度.md，保留手填截止日期、状态与备注；不填写网申表单')
    parser.add_argument('--add', nargs=2, metavar=('公司', 'URL'), action='append', help='手动添加入口，可重复使用')
    args = parser.parse_args(argv)
    manual = [{'company': company.strip(), 'url': url} for company, url in args.add or []]
    if any(not item['company'] or not _valid_url(item['url']) for item in manual):
        parser.error('--add 需要非空公司名及有效的 HTTP(S) URL 或 mailto:邮箱')
    path = report_path(ROOT, '网申进度.md')
    try:
        cache_file = cache_path(ROOT, 'official-cache.json')
        cache = json.loads(cache_file.read_text(encoding='utf-8')) if cache_file.exists() else {}
        if not isinstance(cache, dict):
            raise ValueError('official-cache.json 必须是 JSON 对象')
        previous = parse_tracker(path.read_text(encoding='utf-8')) if path.exists() else {}
        rows = build_rows(cache, previous, manual)
        now = datetime.now().astimezone()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_tracker(rows, now), encoding='utf-8')
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f'网申进度已更新｜入口 {len(rows)} 个｜7 天内截止 {len(_due_rows(rows, now))} 个｜网申进度.md')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
