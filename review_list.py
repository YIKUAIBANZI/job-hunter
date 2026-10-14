#!/usr/bin/env python3
"""离线复核清单：日志为岗位来源，Markdown 只保存人工批准。"""
import argparse
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from shared import load_applied_index, report_path

PLATFORMS = ('boss', 'liepin', 'yingjiesheng', '51job', 'yupao')
REVIEW_FILE = '复核清单.md'


def _valid_id(platform, job_id):
    pattern = r'[A-Za-z0-9_-]+' if platform == 'boss' else r'[0-9]+'
    return platform in PLATFORMS and bool(re.fullmatch(pattern, job_id))


def parse_approved(md_text) -> set[str]:
    """仅接受完整十列表格行；51job 即使手改为勾选也不批准。"""
    approved = set()
    for line in md_text.splitlines():
        line = line.strip()
        if not line.startswith('|') or not line.endswith('|'):
            continue
        cells = [c.strip() for c in line[1:-1].split('|')]
        if len(cells) != 10 or cells[0] not in ('[x]', '[X]'):
            continue
        platform, job_id = cells[1], cells[-1]
        if platform != '51job' and _valid_id(platform, job_id):
            approved.add(f'{platform}:{job_id}')
    return approved


def _timestamp(row):
    try:
        value = datetime.fromisoformat(row.get('ts', ''))
        return value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError, OverflowError):
        return float('-inf')


def collect_review_rows(logs, applied_index) -> list[dict]:
    """logs 为 {日志文件名或平台名: 日志字典}；同 ID 取最新，缺 ts 时取最后一条。"""
    latest = {}
    for name, log in logs.items():
        platform = Path(name).name.split('-', 1)[0]
        if platform not in PLATFORMS or not isinstance(log, dict):
            continue
        reviews = log.get('review', [])
        if not isinstance(reviews, list):
            continue
        for entry in reviews:
            if not isinstance(entry, dict):
                continue
            job_id = str(entry.get('job_id') or entry.get('jobId') or '')
            if not _valid_id(platform, job_id):
                continue
            key = f'{platform}:{job_id}'
            if key in applied_index:
                continue
            row = {**entry, 'platform': platform, 'job_id': job_id}
            if key not in latest or _timestamp(row) >= _timestamp(latest[key]):
                latest[key] = row
    return sorted(latest.values(), key=_timestamp, reverse=True)


def render_review_list(rows, approved) -> str:
    def cell(value):
        # 转义分隔符及换行，岗位原文不能伪造另一行批准记录。
        return html.escape(str(value or ''), quote=False).replace('|', '&#124;').replace('\r', ' ').replace('\n', ' ')

    def metric(value):
        return f'{value:.2f}' if isinstance(value, (int, float)) else '—'

    lines = ['# 复核清单', '', '人工看过后将批准列改为 [x]；重新生成保留勾选，已投递或待核验岗位自动剔除。', '']
    for platform in PLATFORMS:
        section = sorted((r for r in rows if r['platform'] == platform), key=_timestamp, reverse=True)
        if not section:
            continue
        lines += [f'## {platform}', '', '| 批准 | 平台 | 公司 | 岗位 | hard | fit | Jev 建议 | 原因 | 链接 | ID |',
                  '|---|---|---|---|---|---|---|---|---|---|']
        for row in section:
            jev = row.get('jev') or {}
            d = jev.get('decision') or {}
            reason = row.get('reason', '')
            checked = '[x]' if f"{platform}:{row['job_id']}" in approved else '[ ]'
            if platform == '51job':
                checked = '—'
                reason = '51job 暂不支持批准投递，请在页面手动处理；' + reason
            elif row.get('manual_form'):
                # 应届生需填企业表单的岗位：批准投递也点不了，交给用户在平台手动填写
                checked = '—'
                reason = '需在平台填写企业表单，请手动投递；' + reason
            values = [checked, platform, row.get('company', ''), row.get('job', ''),
                      metric(d.get('hard_ok')), metric(d.get('fit_ev')),
                      {'0': '投递', '1': '复核', '2': '跳过'}.get(d.get('action'), '—'),
                      reason, row.get('url', ''), row['job_id']]
            lines.append('| ' + ' | '.join(cell(v) for v in values) + ' |')
        lines.append('')
    return '\n'.join(lines) + '\n'


def load_review_rows(root, city=None):
    """只读取四个平台的岗位日志；去重索引始终覆盖所有城市。"""
    logs = {}
    for path in sorted((root / 'data' / 'logs').glob('*-log.json')):
        match = re.fullmatch(r'(boss|liepin|yingjiesheng|51job|yupao)-(.+)-log\.json', path.name)
        if not match or city is not None and match[2] != city:
            continue
        try:
            logs[path.name] = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
    return collect_review_rows(logs, load_applied_index(root))


def approved_review_rows(root, platform, city):
    path = report_path(root, REVIEW_FILE)
    approved = parse_approved(path.read_text(encoding='utf-8')) if path.exists() else set()
    return [r for r in load_review_rows(root, city)
            if r['platform'] == platform and f"{platform}:{r['job_id']}" in approved
            and not r.get('manual_form')]


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线生成/更新复核清单，保留人工勾选')
    parser.add_argument('--city', help='只收集该城市，默认收集全部城市')
    args = parser.parse_args(argv)
    root = Path(__file__).parent
    path = report_path(root, REVIEW_FILE)
    approved = parse_approved(path.read_text(encoding='utf-8')) if path.exists() else set()
    rows = load_review_rows(root, args.city)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_review_list(rows, approved), encoding='utf-8')
    print(f'复核岗位 {len(rows)} 个：{path}')


if __name__ == '__main__':
    main()
