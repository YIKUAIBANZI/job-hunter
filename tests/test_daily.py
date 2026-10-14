"""日跑离线回归：子进程严格替身，日志和日报全部位于临时目录。"""
import copy
import json
import subprocess
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

import daily


def plan(**overrides):
    return {'city': '深圳', 'platforms': {
        p: {'quota': 0, 'keywords': ['AI产品', '产品实习'], 'args': []}
        for p in daily.PLATFORMS}, 'greet': False, 'inbox': False,
        'review_list': False, **overrides}


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def entry(job_id, **fields):
    return {'job_id': job_id, 'company': '公司A', 'job': 'AI产品',
            'url': 'https://example.test/job/' + str(job_id), **fields}


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(daily, 'ROOT', tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail('日跑测试不允许真实子进程或休息')
    monkeypatch.setattr(daily.subprocess, 'run', forbidden)
    monkeypatch.setattr(daily.time, 'sleep', forbidden)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    (tmp_path / 'data' / 'logs').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'reports').mkdir(parents=True, exist_ok=True)
    calls = []
    actions = []
    def run(command, **kwargs):
        calls.append(command)
        assert kwargs['cwd'] == tmp_path
        assert kwargs['timeout'] == 90 * 60
        assert kwargs['stderr'] == subprocess.STDOUT
        assert kwargs['check'] is False
        assert not any(arg.split('=')[0] in ('--fast', '--ignore-cooldown', '--calibrate') for arg in command)
        assert 'boss-b' not in command and '--match-policy=boss-b' not in command
        action = actions.pop(0) if actions else {}
        if action.get('exception'):
            raise action['exception']
        platform = command[1].removesuffix('_apply.py')
        if platform in daily.PLATFORMS:
            path = tmp_path / 'data' / 'logs' / f'{platform}-深圳-log.json'
            log = json.loads(path.read_text()) if path.exists() else {}
            for bucket, rows in action.get('add', {}).items():
                log.setdefault(bucket, []).extend(rows)
            if action.get('corrupt'):
                path.write_text('{坏日志', encoding='utf-8')
            else:
                write_json(path, log)
        for name, content in action.get('files', {}).items():
            path = tmp_path / 'reports' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        kwargs['stdout'].write(action.get('output', ''))
        return SimpleNamespace(returncode=action.get('code', 0))
    monkeypatch.setattr(daily.subprocess, 'run', run)
    return SimpleNamespace(calls=calls, actions=actions, root=tmp_path,
                           execute=lambda p, **kw: daily.run_daily(p, no_rest=True, **kw))


def test_plan_valid_and_zero_quota(tmp_path):
    data = plan()
    path = tmp_path / 'daily.json'
    write_json(path, data)
    assert daily.load_plan(path, {'target_roles': ['AI产品', '产品实习']}) == data
    assert daily.load_plan(path, {'target_roles': ['AI产品', '产品实习']})['platforms']['boss']['quota'] == 0


@pytest.mark.parametrize('missing', ['city', 'platforms', 'greet', 'inbox', 'review_list',
                                    'boss', 'quota', 'keywords', 'args'])
def test_plan_missing_fields(tmp_path, missing):
    data = plan()
    if missing in data:
        del data[missing]
    elif missing == 'boss':
        del data['platforms']['boss']
    else:
        del data['platforms']['boss'][missing]
    path = tmp_path / 'daily.json'
    write_json(path, data)
    with pytest.raises(ValueError):
        daily.load_plan(path, {'target_roles': ['AI产品', '产品实习']})


@pytest.mark.parametrize('quota', [0, 1])
def test_plan_exact_whitelist_including_disabled(tmp_path, quota):
    data = plan()
    data['platforms']['boss'].update(quota=quota, keywords=['AI产品经理'])
    path = tmp_path / 'daily.json'
    write_json(path, data)
    with pytest.raises(ValueError, match='target_roles'):
        daily.load_plan(path, {'target_roles': ['AI产品', '产品实习']})


@pytest.mark.parametrize('field,value', [('quota', -1), ('quota', True), ('quota', 1.5),
                                        ('keywords', []), ('keywords', 'AI产品'),
                                        ('args', '--fast'), ('args', [1])])
def test_plan_bad_platform_fields(tmp_path, field, value):
    data = plan()
    data['platforms']['boss'][field] = value
    path = tmp_path / 'daily.json'
    write_json(path, data)
    with pytest.raises(ValueError):
        daily.load_plan(path, {'target_roles': ['AI产品', '产品实习']})


@pytest.mark.parametrize('args', [['--fast'], ['--fast=true'], ['--ignore-cooldown'],
                                ['--calibrate'], ['--match-policy', 'boss-b'],
                                ['--match-policy=boss-b'], ['--match-pol', 'boss-b'],
                                ['--count', '999'], ['--job', '越界'], ['--city=北京'],
                                ['--approved'], ['--fill-queue'], ['--dry-run'],
                                ['--experience'], ['--degree', '--calibrate'],
                                ['--refresh=true']])
def test_plan_cannot_smuggle_unsafe_or_overriding_args(tmp_path, args):
    data = plan()
    data['platforms']['boss']['args'] = args
    path = tmp_path / 'daily.json'
    write_json(path, data)
    with pytest.raises(ValueError):
        daily.load_plan(path, {'target_roles': ['AI产品', '产品实习']})


def test_plan_safe_args(tmp_path):
    data = plan()
    args = ['--experience', '实习生', '--degree=本科', '--matcher', 'keyword',
            '--match-policy=review-gate', '--refresh', '--max-views', '2']
    data['platforms']['liepin']['args'] = args
    path = tmp_path / 'daily.json'
    write_json(path, data)
    assert daily.load_plan(path, {'target_roles': ['AI产品', '产品实习']})['platforms']['liepin']['args'] == args


@pytest.mark.parametrize('live', [False, True])
def test_main_mode_order_arguments_and_summary(harness, capsys, live):
    data = plan()
    for p in daily.PLATFORMS:
        data['platforms'][p].update(quota=1, keywords=['AI产品'])
    data['platforms']['liepin']['args'] = ['--experience', '实习生', '--degree', '本科']
    write_json(harness.root / 'config.json', {'target_roles': ['AI产品']})
    write_json(harness.root / 'daily.json', data)
    assert daily.main(['--no-rest', *(['--live'] if live else [])]) == 0
    assert [c[1] for c in harness.calls] == [f'{p}_apply.py' for p in daily.PLATFORMS]
    for command in harness.calls:
        assert command[0] == sys.executable
        assert command[2:8] == ['--job', 'AI产品', '--city', '深圳', '--count', '1']
        assert ('--dry-run' in command) is not live
    assert '--experience' in harness.calls[1]
    assert len(capsys.readouterr().out.splitlines()) == 1
    md = (harness.root / 'reports' / '日报.md').read_text()
    assert ('真投' if live else '试跑') in md
    assert '| boss | 1 |' in md
    logs = list((harness.root / 'data' / 'runs').rglob('*.log'))
    assert len(logs) == 5 and any(p.name == 'yingjiesheng-1.log' for p in logs)
    assert all(f'](../{p.relative_to(harness.root).as_posix()})' in md for p in logs)


def test_yjs_daily_order_example_and_old_plan_rejection(tmp_path):
    from pathlib import Path
    assert daily.PLATFORMS == ('boss','liepin','yingjiesheng','51job','yupao')
    example = json.loads((Path(__file__).resolve().parents[1] / 'daily.example.json').read_text())
    assert example['platforms']['yingjiesheng'] == {'quota':0,'keywords':['产品经理'],'args':[]}
    path = tmp_path / 'daily.json'
    write_json(path, example)
    assert daily.load_plan(path, {'target_roles':['AI产品','产品实习','产品经理']}) == example
    del example['platforms']['yingjiesheng']
    write_json(path, example)
    with pytest.raises(ValueError, match='yingjiesheng'):
        daily.load_plan(path, {'target_roles':['AI产品','产品实习','产品经理']})


def test_invalid_main_plan_stops_before_processes(harness):
    data = plan()
    write_json(harness.root / 'config.json', {'target_roles': []})
    write_json(harness.root / 'daily.json', data)
    with pytest.raises(SystemExit) as exc:
        daily.main(['--live', '--no-rest'])
    assert exc.value.code == 2 and not harness.calls
    assert not (harness.root / 'data' / 'runs').exists()


def test_zero_quota_skips_all_and_report_overwritten(harness):
    (harness.root / 'reports' / '日报.md').write_text('旧日报')
    result = harness.execute(plan())
    assert not harness.calls and result['exit_code'] == 0
    assert all('quota=0，跳过' in row['note'] for row in result['platforms'])
    assert '旧日报' not in (harness.root / 'reports' / '日报.md').read_text()


def test_live_quota_decrements_new_unique_applied(harness):
    data = plan()
    data['platforms']['boss'].update(quota=5, keywords=['AI产品', '产品实习', 'AI产品'])
    write_json(harness.root / 'data' / 'logs' / 'boss-深圳-log.json', {'applied': [entry('old')]})
    harness.actions.extend([
        {'add': {'applied': [entry('old'), entry('a'), entry('a'), entry('b'), entry('c')]}},
        {'add': {'applied': [entry('d'), entry('e')]}}])
    result = harness.execute(data, live=True)
    assert [c[c.index('--count') + 1] for c in harness.calls] == ['5', '2']
    assert result['platforms'][0]['applied'] == 5
    assert result['platforms'][0]['status'] == '配额满'
    assert len(result['platforms'][0]['rows']['applied']) == 5


@pytest.mark.parametrize('outputs', [
    ['🧪 dry-run 已评分: 3 份', '🧪 dry-run 已评分: 2 份'],
    ['boss：成功 0，待核验 0，详情 3，评估 3', 'boss：成功 0，待核验 0，详情 2，评估 2']])
def test_dry_quota_uses_evaluations_not_log_counts(harness, outputs):
    data = plan()
    data['platforms']['boss'].update(quota=5, keywords=['AI产品', '产品实习', 'AI产品'])
    harness.actions.extend({'output': text} for text in outputs)
    result = harness.execute(data)
    assert [c[c.index('--count') + 1] for c in harness.calls] == ['5', '2']
    assert result['platforms'][0]['applied'] == 0
    assert result['platforms'][0]['status'] == '配额满'


@pytest.mark.parametrize('code,status,exit_code', [(2, '冷却', 0), (1, '风控', 1), (3, '异常', 0), (-9, '异常', 0)])
@pytest.mark.parametrize('live', [False, True])
def test_platform_exit_stops_keywords_continues_next(harness, code, status, exit_code, live):
    data = plan()
    data['platforms']['boss']['quota'] = 5
    data['platforms']['liepin'].update(quota=1, keywords=['AI产品'])
    harness.actions.append({'code': code})
    result = harness.execute(data, live=live)
    assert [c[1] for c in harness.calls] == ['boss_apply.py', 'liepin_apply.py']
    assert result['platforms'][0]['status'] == status
    assert result['exit_code'] == exit_code


@pytest.mark.parametrize('row', [entry('blocked', status='blocked'), {'status': 'blocked', 'reason': '列表风控'}])
def test_new_blocked_overrides_zero_or_cooldown_exit(harness, row):
    data = plan()
    data['platforms']['liepin']['quota'] = 2
    data['platforms']['yupao'].update(quota=1, keywords=['AI产品'])
    harness.actions.append({'code': 2, 'add': {'failed': [row]}})
    result = harness.execute(data, live=True)
    assert [c[1] for c in harness.calls] == ['liepin_apply.py', 'yupao_apply.py']
    assert result['platforms'][1]['status'] == '风控'
    assert result['exit_code'] == 1


@pytest.mark.parametrize('code', [0, 1, 2, 3])
def test_live_uncalibrated_message_even_nonzero(harness, code):
    data = plan()
    data['platforms']['yupao']['quota'] = 2
    harness.actions.append({'code': code, 'output': '停止原因：该平台尚未首单校准；请先 dry-run'})
    result = harness.execute(data, live=True)
    assert len(harness.calls) == 1
    assert result['platforms'][daily.PLATFORMS.index('yupao')]['status'] == '未校准'
    assert '未校准，跳过' in result['platforms'][daily.PLATFORMS.index('yupao')]['note']
    assert result['exit_code'] == 0


def test_current_entry_cooldown_zero_exit(harness):
    data = plan()
    data['platforms']['51job']['quota'] = 3
    harness.actions.append({'output': '停止原因：风控冷却中，剩余 5 分钟；未连接浏览器'})
    result = harness.execute(data)
    assert len(harness.calls) == 1 and result['platforms'][daily.PLATFORMS.index('51job')]['status'] == '冷却'


def test_pending_stops_next_keyword_without_retry(harness):
    data = plan()
    data['platforms']['boss']['quota'] = 2
    harness.actions.append({'add': {'pending': [entry('p')]}})
    result = harness.execute(data, live=True)
    assert len(harness.calls) == 1
    assert result['platforms'][0]['pending'] == 1
    assert '人工核验' in result['platforms'][0]['note']


def test_timeout_preserves_partial_log_and_continues(harness, monkeypatch):
    data = plan()
    data['platforms']['boss']['quota'] = 3
    data['platforms']['liepin'].update(quota=1, keywords=['AI产品'])
    original = daily.subprocess.run
    def run(command, **kwargs):
        if command[1] == 'boss_apply.py':
            harness.calls.append(command)
            write_json(harness.root / 'data' / 'logs' / 'boss-深圳-log.json', {'pending': [entry('uncertain')]})
            raise subprocess.TimeoutExpired(command, daily.TIMEOUT)
        return original(command, **kwargs)
    monkeypatch.setattr(daily.subprocess, 'run', run)
    result = harness.execute(data, live=True)
    assert result['platforms'][0]['pending'] == 1
    assert result['platforms'][0]['status'] == '异常'
    assert len(harness.calls) == 2
    assert '90 分钟' in next((harness.root / 'data' / 'runs').rglob('boss-1.log')).read_text()


@pytest.mark.parametrize('when', ['before', 'after'])
def test_bad_log_stops_only_platform(harness, when):
    data = plan()
    data['platforms']['boss']['quota'] = 3
    data['platforms']['liepin'].update(quota=1, keywords=['AI产品'])
    if when == 'before':
        (harness.root / 'data' / 'logs' / 'boss-深圳-log.json').write_text('{')
    else:
        harness.actions.append({'corrupt': True})
    result = harness.execute(data)
    assert result['platforms'][0]['status'] == '异常'
    assert harness.calls[-1][1] == 'liepin_apply.py'


def test_platform_rest_random_in_dry_mode(harness, monkeypatch):
    data = plan()
    data['platforms']['boss'].update(quota=1, keywords=['AI产品'])
    data['platforms']['51job'].update(quota=1, keywords=['AI产品'])
    rests = []
    ranges = []
    monkeypatch.setattr(daily.random, 'uniform', lambda lo, hi: ranges.append((lo, hi)) or 123)
    monkeypatch.setattr(daily.time, 'sleep', rests.append)
    daily.run_daily(data)
    assert rests == [123] and ranges == [(60, 180)]


@pytest.mark.parametrize('live', [False, True])
def test_post_steps_review_increment_inbox_header(harness, live):
    data = plan(greet=True, review_list=True, inbox=True)
    old_row = '| [x] | boss | A | 产品 | 1 | 2 | 复核 | 原因 | url | old |\n'
    new_row = '| [ ] | liepin | B | 产品 | 1 | 2 | 复核 | 原因 | url | new |\n'
    (harness.root / 'reports' / '复核清单.md').write_text(old_row)
    if live:
        harness.actions.append({})
    harness.actions.extend([
        {'files': {'复核清单.md': '# 复核清单\n' + old_row + new_row}},
        {'files': {'回复日报.md': '# 回复日报\n\n生成时间：今天\n\nBoss：会话 8｜HR 已回复 4｜已读未回 2\n'}}])
    result = harness.execute(data, live=live)
    expected = (['boss_greet.py'] if live else []) + ['review_list.py', 'inbox_report.py']
    assert [c[1] for c in harness.calls] == expected
    if live:
        assert harness.calls[0][2:] == ['--city', '深圳', '--send', '--count', '10']
    assert result['review_added'] == 1 and result['hr_replied'] == 4
    assert '本次读取' in result['inbox_note']
    md = (harness.root / 'reports' / '日报.md').read_text()
    assert '复核清单新增：1' in md and 'HR 已回复：4' in md


def test_failed_inbox_reports_old_file_as_stale(harness):
    data = plan(inbox=True)
    (harness.root / 'reports' / '回复日报.md').write_text('# 回复日报\nBoss：会话 3｜HR 已回复 2')
    harness.actions.append({'code': 1, 'output': '风控冷却中，日报未更新'})
    result = harness.execute(data)
    assert result['hr_replied'] == 2 and '非本次更新' in result['inbox_note']
    assert result['exit_code'] == 0


def test_post_inbox_wind_control_changes_overall_exit(harness):
    data = plan(inbox=True)
    harness.actions.append({'code': 1, 'output': '风控：验证弹层；停止，日报未更新'})
    result = harness.execute(data)
    assert result['exit_code'] == 1 and result['platforms'][0]['status'] == '风控'


def test_unreadable_optional_reports_do_not_lose_daily(harness):
    data = plan(review_list=True, inbox=True)
    (harness.root / 'reports' / '复核清单.md').write_bytes(b'\xff')
    (harness.root / 'reports' / '回复日报.md').write_bytes(b'\xff')
    result = harness.execute(data)
    assert result['review_added'] == '未能核算'
    assert result['hr_replied'] is None
    md = (harness.root / 'reports' / '日报.md').read_text()
    assert '新复核清单不可读' in md and '回复日报不可读' in md
    assert 'HR 已回复：未读取' in md


def test_diff_only_new_per_bucket_ids_dedup_and_no_mutation():
    before = {'applied': [entry('a')], 'pending': [entry('p')], 'review': [entry('r')],
              'failed': [entry('f', status='blocked')]}
    after = {'applied': [entry('a'), entry('b'), entry('b'), {'jobId': 'legacy'}],
             'pending': [entry('p'), entry('b'), entry('q'), entry('q')],
             'review': [entry('r'), entry('s'), entry('s')],
             'skipped': [entry('t'), entry('t'), {}],
             'failed': [entry('f', status='blocked', ts='新时间'), entry('u'), entry('v', status='blocked')]}
    original = copy.deepcopy((before, after))
    result = daily.diff_logs(before, after)
    assert [result[k] for k in daily.BUCKETS] == [2, 1, 1, 1, 2]
    assert result['blocked'] == 1
    assert result['rows']['applied'][-1]['jobId'] == 'legacy'
    assert (before, after) == original


def test_diff_bucket_transition_counts_new_applied():
    result = daily.diff_logs({'pending': [entry('p')], 'review': [entry('r')]},
                             {'applied': [entry('p'), entry('r')]})
    assert result['applied'] == 2 and result['pending'] == 0


def test_render_all_sections_duration_rows_and_escaping():
    row = {'platform': 'boss', 'quota': 5, 'status': '风控',
           'logs': ['data/runs/2026-10-14/boss-1.log'], 'note': '风控，停止',
           **daily.diff_logs({}, {'applied': [entry('a', company='公司|A\n<img>')],
                                  'pending': [entry('b')], 'review': [entry('c')]})}
    result = {'platforms': [row], 'elapsed_seconds': 3661, 'review_added': 1,
              'hr_replied': 3, 'inbox_note': '本次读取 回复日报.md'}
    original = copy.deepcopy(result)
    md = daily.render_daily(result, datetime(2026, 10, 14), '真投')
    assert '# 日报 2026-10-14｜真投｜总耗时 1小时1分1秒' in md
    assert all(f'## {heading}' in md for heading in ('平台', '需要你处理', '本次新增已投', '运行说明'))
    assert '| boss | 5 | 1 | 1 | 1 | 0 | 风控 |' in md
    assert '](../data/runs/2026-10-14/boss-1.log)' in md
    assert '待核验：1' in md and '复核清单新增：1' in md and '--approved' in md
    assert 'HR 已回复：3' in md and '回复日报.md' in md
    assert '| boss | 公司&#124;A &lt;img&gt; | AI产品 | https://example.test/job/a |' in md
    assert result == original


def test_diff_logs_counts_appended_review_and_skip_even_if_seen_before():
    from daily import diff_logs
    before = {'review': [{'job_id': 'a'}], 'skipped': [{'job_id': 's'}], 'applied': [{'job_id': 'x'}]}
    after = {'review': [{'job_id': 'a'}, {'job_id': 'a'}, {'job_id': 'b'}],
             'skipped': [{'job_id': 's'}, {'job_id': 's'}],
             'applied': [{'job_id': 'x'}, {'job_id': 'x'}]}
    result = diff_logs(before, after)
    assert result['review'] == 2 and result['skipped'] == 1
    assert result['applied'] == 0  # 已投仍按新 ID 计


@pytest.mark.parametrize('value', [True, False])
def test_plan_optional_tracker_boolean(tmp_path, value):
    path = tmp_path / 'daily.json'
    write_json(path, plan(tracker=value))
    assert daily.load_plan(path, {'target_roles': ['AI产品', '产品实习']})['tracker'] is value


@pytest.mark.parametrize('value', [1, None, 'true', []])
def test_plan_rejects_invalid_tracker(tmp_path, value):
    path = tmp_path / 'daily.json'
    write_json(path, plan(tracker=value))
    with pytest.raises(ValueError, match='tracker'):
        daily.load_plan(path, {'target_roles': ['AI产品', '产品实习']})


@pytest.mark.parametrize('live', [False, True])
def test_tracker_post_step_order_and_report(harness, live):
    from apply_tracker import render_tracker
    harness.actions.extend([{}, {}, {'files': {'网申进度.md': render_tracker([], datetime(2026, 10, 14))}}])
    result = harness.execute(plan(review_list=True, inbox=True, tracker=True), live=live)
    assert [command[1] for command in harness.calls] == ['review_list.py', 'inbox_report.py', 'apply_tracker.py']
    assert harness.calls[-1] == [sys.executable, 'apply_tracker.py']
    assert result['tracker'] == {'entries': 0, 'due': 0, 'note': '本次更新，详见 网申进度.md'}
    assert '网申入口 0 个，7 天内截止 0 个' in (harness.root / 'reports' / '日报.md').read_text()


def test_tracker_reads_only_top_statistics_not_notes(harness):
    md = ('# 网申进度\n\n生成时间：今天\n'
          '统计：入口数 12｜未投 4｜已投 8\n即将截止（7 天内）：3 个\n'
          + '\n' * 10 + '统计：入口数 999\n即将截止（7 天内）：999 个\n')
    harness.actions.append({'files': {'网申进度.md': md}})
    result = harness.execute(plan(tracker=True))
    assert result['tracker']['entries'] == 12 and result['tracker']['due'] == 3
    assert '网申入口 12 个，7 天内截止 3 个' in (harness.root / 'reports' / '日报.md').read_text()


@pytest.mark.parametrize('enabled', [None, False])
def test_tracker_default_off(harness, enabled):
    data = plan() if enabled is None else plan(tracker=enabled)
    result = harness.execute(data)
    assert not harness.calls and 'tracker' not in result
    assert '网申入口' not in (harness.root / 'reports' / '日报.md').read_text()


def test_tracker_failure_marks_existing_statistics_stale(harness):
    (harness.root / 'reports' / '网申进度.md').write_text('统计：入口数 8｜未投 8\n即将截止（7 天内）：2 个\n')
    harness.actions.append({'code': 2})
    result = harness.execute(plan(tracker=True))
    assert result['tracker']['entries'] == 8 and '非本次更新' in result['tracker']['note']
    md = (harness.root / 'reports' / '日报.md').read_text()
    assert '网申入口 8 个，7 天内截止 2 个' in md and r'apply\_tracker：退出码 2' in md


@pytest.mark.parametrize('content', [None, '没有统计', b'\xff'])
def test_tracker_missing_or_unreadable_report_still_generates_daily(harness, content):
    if isinstance(content, bytes):
        (harness.root / 'reports' / '网申进度.md').write_bytes(content)
    elif content is not None:
        (harness.root / 'reports' / '网申进度.md').write_text(content)
    result = harness.execute(plan(tracker=True))
    assert result['tracker']['entries'] is None
    assert '网申入口：未读取统计' in (harness.root / 'reports' / '日报.md').read_text()
