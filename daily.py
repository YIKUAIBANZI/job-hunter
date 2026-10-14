#!/usr/bin/env python3
"""日跑编排：只调用现有 CLI，默认试跑，结束后覆盖生成日报。"""
import argparse
import html
import json
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLATFORMS = ('boss', 'liepin', '51job', 'yupao')
BUCKETS = ('applied', 'pending', 'review', 'skipped', 'failed')
TIMEOUT = 90 * 60
# 只允许筛选和匹配参数，禁止覆盖编排的城市、关键词、配额和模式。
VALUE_ARGS = {'--job-type', '--experience', '--degree', '--matcher', '--min-score',
              '--max-views', '--max-pages', '--match-policy'}
FLAG_ARGS = {'--refresh'}


def load_plan(path, cfg) -> dict:
    """读取完整计划；所有关键词（含零配额平台）都必须精确属于白名单。"""
    plan = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(plan, dict):
        raise ValueError('计划必须是 JSON 对象')
    for key in ('city', 'platforms', 'greet', 'inbox', 'review_list'):
        if key not in plan:
            raise ValueError(f'计划缺字段：{key}')
    city = plan['city']
    if not isinstance(city, str) or not city.strip() or any(c in city for c in '/\\\r\n') or city.startswith('-'):
        raise ValueError('city 必须是非空城市名，不能含路径分隔符或换行')
    for key in ('greet', 'inbox', 'review_list'):
        if type(plan[key]) is not bool:
            raise ValueError(f'{key} 必须是布尔值')
    if 'tracker' in plan and type(plan['tracker']) is not bool:
        raise ValueError('tracker 必须是布尔值')
    roles = cfg.get('target_roles') if isinstance(cfg, dict) else None
    if not isinstance(roles, list) or any(not isinstance(role, str) for role in roles):
        raise ValueError('config.json target_roles 必须是字符串列表')
    platforms = plan['platforms']
    if not isinstance(platforms, dict) or set(platforms) != set(PLATFORMS):
        raise ValueError('platforms 必须包含且仅包含 boss、liepin、51job、yupao')
    for platform in PLATFORMS:
        item = platforms[platform]
        if not isinstance(item, dict) or not {'quota', 'keywords', 'args'} <= item.keys():
            raise ValueError(f'{platform} 缺字段：quota / keywords / args')
        if type(item['quota']) is not int or item['quota'] < 0:
            raise ValueError(f'{platform} quota 必须是非负整数')
        keywords = item['keywords']
        if not isinstance(keywords, list) or not keywords or any(
                not isinstance(kw, str) or not kw.strip() or kw not in roles for kw in keywords):
            raise ValueError(f'{platform} keywords 必须是 config.json target_roles 的非空子集')
        args = item['args']
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise ValueError(f'{platform} args 必须是字符串列表')
        i = 0
        while i < len(args):
            flag, sep, inline = args[i].partition('=')
            if flag in FLAG_ARGS and not sep:
                i += 1
                continue
            if flag not in VALUE_ARGS:
                raise ValueError(f'{platform} 禁止或不支持的参数：{flag}')
            value = inline if sep else (args[i + 1] if i + 1 < len(args) else '')
            if not value or value.startswith('-'):
                raise ValueError(f'{platform} {flag} 缺少有效值')
            if flag == '--match-policy' and value != 'review-gate':
                raise ValueError('日跑只允许 --match-policy review-gate')
            i += 1 if sep else 2
    return plan


def _job_id(row):
    return str(row.get('job_id') or row.get('jobId') or '')


def diff_logs(before, after) -> dict:
    """按分类内新增 ID 计数；重复追加不多计，成功后的预记不算待核验。"""
    result = {}
    rows = {}
    applied_ids = {_job_id(row) for row in after.get('applied', [])}
    for bucket in BUCKETS:
        old_ids = {_job_id(row) for row in before.get(bucket, [])}
        if bucket in ('review', 'skipped'):
            # 复核/跳过按本次追加的记录计（缓存命中的旧岗位也算本次评估结果），同 ID 只计一次
            old_ids = set()
            appended = after.get(bucket, [])[len(before.get(bucket, [])):]
        else:
            appended = after.get(bucket, [])
        unique = {}
        for row in appended:
            job_id = _job_id(row)
            if job_id and job_id not in old_ids and not (bucket == 'pending' and job_id in applied_ids):
                unique[job_id] = dict(row)
        rows[bucket] = list(unique.values())
        result[bucket] = len(unique)
    # 页面级风控可能尚未定位岗位，不能因缺 ID 漏掉停止信号。
    old_blocked = {(_job_id(row) or json.dumps(row, sort_keys=True, ensure_ascii=False))
                   for row in before.get('failed', []) if row.get('status') == 'blocked'}
    new_blocked = {(_job_id(row) or json.dumps(row, sort_keys=True, ensure_ascii=False))
                   for row in after.get('failed', []) if row.get('status') == 'blocked'
                   and (_job_id(row) or json.dumps(row, sort_keys=True, ensure_ascii=False)) not in old_blocked}
    result['blocked'] = len(new_blocked)
    result['rows'] = rows
    return result


def _cell(value):
    text = html.escape(str(value), quote=False)
    text = re.sub(r'([\\`*_\[\]])', r'\\\1', text)
    return text.replace('|', '&#124;').replace('\r', ' ').replace('\n', ' ')


def render_daily(results, now, mode) -> str:
    """只渲染传入的本轮事实，不读文件或当前时间。"""
    date = now.strftime('%Y-%m-%d') if isinstance(now, datetime) else str(now)
    seconds = int(results.get('elapsed_seconds', 0))
    duration = f'{seconds // 3600}小时{seconds % 3600 // 60}分{seconds % 60}秒'
    platforms = results['platforms']
    pending = sum(row['pending'] for row in platforms)
    review = sum(row['review'] for row in platforms)
    replied = results.get('hr_replied')
    review_added = results.get('review_added', 0)
    review_label = f'{review_added} 份' if isinstance(review_added, int) else str(review_added)
    lines = [f'# 日报 {date}｜{mode}｜总耗时 {duration}', '', '## 平台', '',
             '| 平台 | 配额 | 新增已投 | 待核验 | 复核 | 跳过 | 状态 | 日志文件 |',
             '|---|---|---|---|---|---|---|---|']
    for row in platforms:
        logs = '、'.join(f'[{Path(path).name}]({path})' for path in row['logs']) or '—'
        values = [row['platform'], row['quota'], row['applied'], row['pending'],
                  row['review'], row['skipped'], row['status']]
        lines.append('| ' + ' | '.join(_cell(v) for v in values) + f' | {logs} |')
    lines += ['', '## 需要你处理', '',
              f'- 待核验：{pending} 份，须到对应平台人工核验；未确认前不要重复投递。',
              f'- 复核清单新增：{review_label}（日志新增复核 {review} 份）。'
              '打开 `复核清单.md`，人工看过后打勾，再用对应入口 `--approved`；51job 请在页面手动处理。',
              f'- HR 已回复：{replied if replied is not None else "未读取"}；'
              f'{results.get("inbox_note", "详见 回复日报.md")}。']
    if 'tracker' in results:
        tracker = results['tracker']
        summary = (f'网申入口 {tracker["entries"]} 个，7 天内截止 {tracker["due"]} 个'
                   if tracker.get('entries') is not None else '网申入口：未读取统计')
        lines.append(f'- {summary}；{tracker["note"]}。')
    lines += ['', '## 本次新增已投', '', '| 平台 | 公司 | 岗位 | 链接 |', '|---|---|---|---|']
    total = 0
    for row in platforms:
        for entry in row['rows']['applied']:
            values = [row['platform'], entry.get('company', ''), entry.get('job', ''), entry.get('url', '')]
            lines.append('| ' + ' | '.join(_cell(v) for v in values) + ' |')
            total += 1
    if not total:
        lines += ['', '本次无新增已投。']
    notes = [f'{row["platform"]}：{row["note"]}' for row in platforms if row.get('note')]
    notes += results.get('notes', [])
    if notes:
        lines += ['', '## 运行说明', ''] + [f'- {_cell(note)}' for note in notes]
    return '\n'.join(lines) + '\n'


def _read_log(path):
    log = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if not isinstance(log, dict) or any(not isinstance(log.get(bucket, []), list) or
            any(not isinstance(row, dict) for row in log.get(bucket, [])) for bucket in BUCKETS):
        raise ValueError(f'岗位日志结构错误：{path.name}')
    return log


def _run(command, path, root):
    """阻塞等待一次子进程；日志合流到磁盘，超时由 subprocess 终止子进程。"""
    with path.open('w', encoding='utf-8') as output:
        try:
            completed = subprocess.run(command, cwd=root, stdout=output,
                                       stderr=subprocess.STDOUT, timeout=TIMEOUT, check=False)
            code = completed.returncode
        except subprocess.TimeoutExpired:
            output.write('\n日跑：子进程超时（90 分钟），已终止，不重试。\n')
            code = -1
        except OSError as exc:
            output.write(f'\n日跑：无法启动子进程：{type(exc).__name__}\n')
            code = -1
    return code, path.read_text(encoding='utf-8', errors='replace')


def _status(code, output, blocked, live):
    if blocked:
        return '风控'
    # 首单门禁的明确文案优先于普通退出码，兼容未来入口用非零表示拒绝。
    if live and '尚未首单校准' in output:
        return '未校准'
    if code == 1:
        return '风控'
    if code == 2 or '冷却中' in output:
        return '冷却'
    return '异常' if code else '完成'


def _review_ids(path):
    if not path.exists():
        return set()
    keys = set()
    for line in path.read_text(encoding='utf-8').splitlines():
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if len(cells) == 10 and cells[1] in PLATFORMS and cells[-1]:
            keys.add((cells[1], cells[-1]))
    return keys


def run_daily(plan, *, live=False, no_rest=False, root=None) -> dict:
    """顺序编排并收集本轮报告数据；不调用入口的任何内部函数。"""
    root = Path(root) if root is not None else ROOT
    started = time.monotonic()
    now = datetime.now().astimezone()
    run_dir = root / 'runs' / now.strftime('%Y-%m-%d')
    run_dir.mkdir(parents=True, exist_ok=True)
    results = {'platforms': [], 'notes': [], 'review_added': 0, 'hr_replied': None}
    ran_platform = False
    for platform in PLATFORMS:
        item = plan['platforms'][platform]
        row = {'platform': platform, 'quota': item['quota'], 'logs': [],
               'status': '完成', 'note': '', **diff_logs({}, {})}
        results['platforms'].append(row)
        if not item['quota']:
            row['note'] = 'quota=0，跳过'
            continue
        if ran_platform and not no_rest:
            time.sleep(random.uniform(60, 180))
        ran_platform = True
        log_path = root / f'{platform}-{plan["city"]}-log.json'
        try:
            baseline = _read_log(log_path)
        except (OSError, ValueError) as exc:
            row.update(status='异常', note=f'启动前日志不可读：{exc}')
            continue
        previous = baseline
        evaluated = 0
        for seq, keyword in enumerate(item['keywords'], 1):
            used = row['applied'] if live else evaluated
            remaining = item['quota'] - used
            if remaining <= 0:
                row['status'] = '配额满'
                break
            command = [sys.executable, f'{platform}_apply.py', '--job', keyword,
                       '--city', plan['city'], '--count', str(remaining), *item['args']]
            if not live:
                command.append('--dry-run')
            path = run_dir / f'{platform}-{seq}.log'
            row['logs'].append(path.relative_to(root).as_posix())
            code, output = _run(command, path, root)
            try:
                current = _read_log(log_path)
                step = diff_logs(previous, current)
                row.update(diff_logs(baseline, current))
                previous = current
            except (OSError, ValueError) as exc:
                row.update(status='风控' if code == 1 else '异常', note=f'运行后日志不可读：{exc}')
                break
            row['status'] = _status(code, output, step['blocked'], live)
            reasons = re.findall(r'停止原因[：:]\s*([^\n]+)', output)
            row['note'] = reasons[-1] if reasons else ''
            if row['status'] == '未校准':
                row['note'] = '未校准，跳过；日跑不做首单校准'
            elif row['status'] == '冷却':
                row['note'] = '冷却中，跳过剩余关键词'
            elif row['status'] != '完成':
                row['note'] = f'{row["status"]}，停止该平台（退出码 {code}）；' + row['note']
            if row['status'] != '完成':
                break
            if not live:
                matches = re.findall(r'(?:dry-run 已评分[：:]\s*|评估\s*)(\d+)', output)
                # 老入口没有评估摘要时，按本次传入额度消费，避免反复扫描。
                evaluated += min(remaining, int(matches[-1])) if matches else remaining
            if (row['applied'] if live else evaluated) >= item['quota']:
                row['status'] = '配额满'
                break
            if row['pending']:
                row['note'] = '本次有待核验，停止剩余关键词，请人工核验后再跑'
                break

    # 后处理也只经 CLI；失败写入日报，不将旧文件伪称本轮更新。
    steps = []
    if live and plan['greet']:
        steps.append(('boss_greet', ['--city', plan['city'], '--send', '--count', '10']))
    if plan['review_list']:
        steps.append(('review_list', ['--city', plan['city']]))
    if plan['inbox']:
        steps.append(('inbox_report', ['--city', plan['city']]))
    if plan.get('tracker', False):
        steps.append(('apply_tracker', []))
    try:
        old_reviews = _review_ids(root / '复核清单.md') if plan['review_list'] else set()
    except (OSError, ValueError) as exc:
        old_reviews = None
        results['review_added'] = '未能核算'
        results['notes'].append(f'原复核清单不可读，无法核算新增数：{type(exc).__name__}')
    inbox_ok = False
    for name, args in steps:
        path = run_dir / f'{name}.log'
        code, output = _run([sys.executable, f'{name}.py', *args], path, root)
        note = f'{name}：退出码 {code}；日志 {path.relative_to(root).as_posix()}'
        results['notes'].append(note)
        if name == 'review_list' and code == 0:
            try:
                new_reviews = _review_ids(root / '复核清单.md')
                results['review_added'] = len(new_reviews - old_reviews) if old_reviews is not None else '未能核算'
            except (OSError, ValueError) as exc:
                results['review_added'] = '未能核算'
                results['notes'].append(f'新复核清单不可读：{type(exc).__name__}')
        if name == 'inbox_report':
            inbox_ok = code == 0
        if name == 'apply_tracker':
            tracker = {'entries': None, 'due': None, 'note': '网申进度未生成或首部统计不可读'}
            try:
                # 仅取文件顶部统计，正文中的备注不能影响日报计数。
                header = '\n'.join((root / '网申进度.md').read_text(encoding='utf-8').splitlines()[:10])
                entries = re.search(r'^统计：入口数\s+(\d+)(?:｜|$)', header, re.M)
                due = re.search(r'^即将截止（7 天内）：\s*(\d+)\s*个\s*$', header, re.M)
                if entries and due:
                    tracker.update(entries=int(entries[1]), due=int(due[1]),
                                   note='本次更新，详见 网申进度.md' if code == 0 else
                                   '已有网申进度，非本次更新，详见 网申进度.md')
            except (OSError, ValueError) as exc:
                results['notes'].append(f'网申进度不可读：{type(exc).__name__}')
            results['tracker'] = tracker
        if name in ('boss_greet', 'inbox_report') and '风控：' in output:
            results['platforms'][0].update(status='风控', note=f'{name} 检测到风控，详见后处理日志')
    if not plan['review_list']:
        results['notes'].append('未启用复核清单生成')
    inbox_path = root / '回复日报.md'
    if inbox_path.exists():
        try:
            header = '\n'.join(inbox_path.read_text(encoding='utf-8').splitlines()[:12])
            # Boss 与猎聘各一行统计，合计
            found = re.findall(r'HR\s*已回复\s*(\d+)', header)
            results['hr_replied'] = sum(map(int, found)) if found else None
        except (OSError, ValueError) as exc:
            results['notes'].append(f'回复日报不可读：{type(exc).__name__}')
    results['inbox_note'] = ('本次读取，详见 回复日报.md' if inbox_ok and results['hr_replied'] is not None else
                             '已有回复日报，非本次更新' if results['hr_replied'] is not None else
                             '回复日报未生成或首部统计不可读')
    results['elapsed_seconds'] = time.monotonic() - started
    results['exit_code'] = int(any(row['status'] == '风控' for row in results['platforms']))
    (root / '日报.md').write_text(render_daily(results, now, '真投' if live else '试跑'), encoding='utf-8')
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description='一键日跑并生成日报；默认只读试跑，--live 才真投')
    parser.add_argument('--plan', default='daily.json', help='计划文件（默认 daily.json，相对仓库目录）')
    parser.add_argument('--live', action='store_true', help='真正投递，仅用于已授权的平台和配额')
    parser.add_argument('--no-rest', action='store_true', help='跳过平台间休息，仅供测试/调试')
    args = parser.parse_args(argv)
    try:
        cfg = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
        path = Path(args.plan)
        plan = load_plan(path if path.is_absolute() else ROOT / path, cfg)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    results = run_daily(plan, live=args.live, no_rest=args.no_rest)
    total = sum(row['applied'] for row in results['platforms'])
    print(f'日跑结束｜{"真投" if args.live else "试跑"}｜新增已投 {total}｜'
          f'耗时 {int(results["elapsed_seconds"])} 秒｜退出码 {results["exit_code"]}｜日报.md')
    return results['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
