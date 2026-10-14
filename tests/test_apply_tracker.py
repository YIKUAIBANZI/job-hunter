"""网申进度离线回归；CLI 的读写仅发生在临时目录。"""
import copy
import json
import socket
import subprocess
from datetime import datetime

import pytest

import apply_tracker as tracker

NOW = datetime(2026, 10, 14, 12, 0)


def candidate(path, status='已核验', official=0.8, **extra):
    return {'url': 'https://careers.example.test/' + path, 'title': '校园招聘',
            'status': status, 'official': official, **extra}


def cache(*candidates):
    return {'公司a': {'ts': '2026-10-14T10:00:00', 'result': {
        'company': '公司A', 'jobs': ['AI产品', 'Agent实习'],
        'boss_urls': [], 'candidates': list(candidates)}}}


def row(path='campus', **fields):
    url = 'https://careers.example.test/' + path
    return {'key': tracker.url_key(url), 'company': '公司A', 'url': url,
            'title': '校园招聘', 'verification': '已核验', 'deadline': '',
            'status': '未投', 'note': '', 'jobs': 'AI产品、Agent实习',
            'expired': False, **fields}


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(tracker, 'ROOT', tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail('网申进度不允许联网或启动子进程')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)


@pytest.mark.parametrize('url,key', [
    ('https://Example.TEST/jobs/?from=boss#form', 'https://example.test/jobs'),
    ('HTTPS://Example.TEST/', 'https://example.test'),
    (' http://example.test:8080/Job///?a=1 ', 'http://example.test:8080/Job'),
    ('https://example.test/A%2FB?x=1', 'https://example.test/A%2FB')])
def test_url_key(url, key):
    assert tracker.url_key(url) == key
    assert tracker.url_key(key) == key


def test_parse_render_roundtrip_with_escaping_and_expiry():
    rows = [row('job/(x)?a=1&b=2|3', company='公司|A <集团>',
                title='招聘[入口]*', note='备注|含 \\ 路径 [文本]\n第二行 & <tag>',
                deadline='2026-10-18', status='自行复核', expired=True)]
    original = copy.deepcopy(rows)
    md = tracker.render_tracker(rows, NOW)
    parsed = tracker.parse_tracker(md)
    assert list(parsed.values()) == rows
    assert tracker.render_tracker(list(parsed.values()), NOW) == md
    assert rows == original
    assert '| 公司&#124;A &lt;集团&gt; |' in md
    assert '(缓存已过期)' in md


def test_parse_handwritten_link_escaped_pipe_and_only_main_table():
    md = ('# 网申进度\n\n| 公司 | 入口 | 核验 | 截止日期 | 状态 | 备注 | 来源岗位 |\n'
          '|---|---|---|---|---|---|---|\n'
          '| A | [招聘](https://example.test/jobs/?x=1) | 待核验 | 2026-10-21 | 已投 | 手填\\|备注 | AI产品 |\n'
          '\n| B | [其他](https://other.test) | 已核验 | | 未投 | | |\n')
    parsed = tracker.parse_tracker(md)
    assert list(parsed) == ['https://example.test/jobs']
    assert parsed['https://example.test/jobs']['note'] == '手填|备注'
    assert tracker.parse_tracker('') == {}


def test_rebuild_preserves_fields_and_uses_latest_metadata():
    previous = tracker.parse_tracker(tracker.render_tracker([
        row('campus/?old=1', deadline='2026-10-19', status='面试', note='等通知', expired=True)], NOW))
    current = cache(candidate('campus?new=2', title='新招聘入口', status='待核验'))
    original = copy.deepcopy((current, previous))
    rows = tracker.build_rows(current, previous, [])
    assert len(rows) == 1
    assert [rows[0][field] for field in ('deadline', 'status', 'note')] == ['2026-10-19', '面试', '等通知']
    assert rows[0]['title'] == '新招聘入口' and rows[0]['verification'] == '待核验'
    assert rows[0]['jobs'] == 'AI产品、Agent实习' and not rows[0]['expired']
    assert (current, previous) == original


@pytest.mark.parametrize('fields,retained', [
    ({}, False), ({'deadline': '2026-10-21'}, False),
    ({'status': '已投'}, True), ({'status': '结束'}, True),
    ({'status': '异常原文'}, True), ({'note': '留意后续'}, True),
    ({'verification': '手动'}, True)])
def test_missing_cache_retention(fields, retained):
    old = row(**fields)
    previous = {old['key']: old}
    rows = tracker.build_rows({}, previous, [])
    assert bool(rows) is retained
    if retained:
        assert rows[0]['expired'] is (old['verification'] != '手动')
        rebuilt = tracker.build_rows({}, tracker.parse_tracker(tracker.render_tracker(rows, NOW)), [])
        assert rebuilt == rows


def test_limit_order_and_unique_url_per_company():
    current = cache(candidate('pending', '待核验', 1), candidate('weak', official=.7),
                    candidate('best/?x=1', official=.9), candidate('best?x=2', official=.95),
                    candidate('no', '未找到', 1))
    current['b'] = {'result': {'company': 'B', 'jobs': [], 'candidates': [candidate('b', '待核验', .5)]}}
    current['c'] = {'result': {'company': 'C', 'jobs': [], 'candidates': [candidate('c', '其他', 1)]}}
    rows = tracker.build_rows(current, {}, [])
    assert [entry['key'].split('/')[-1] for entry in rows] == ['best', 'weak', 'b']
    assert rows[0]['url'].endswith('best?x=2')
    assert 'C' not in [entry['company'] for entry in rows]


def test_normalized_companies_share_limit_and_pending_probability_order():
    current = cache(candidate('one', '待核验', .1))
    current['alias'] = {'result': {'company': ' 公司 A ', 'jobs': [], 'candidates': [
        candidate('two', '待核验', .8), candidate('three', '待核验', .9)]}}
    assert [r['key'].split('/')[-1] for r in tracker.build_rows(current, {}, [])] == ['three', 'two']


def test_invalid_candidates_skipped():
    current = cache(candidate('bad', url='javascript:alert(1)'),
                    candidate('good', official=float('nan')), None)
    assert len(tracker.build_rows(current, {}, [])) == 1


def test_status_statistics_preserve_other_and_blank():
    states = [*tracker.STATES, '等待反馈', '']
    rows = [row(str(i), status=state) for i, state in enumerate(states)]
    md = tracker.render_tracker(rows, NOW)
    header = next(line for line in md.splitlines() if line.startswith('统计：'))
    assert header == '统计：入口数 8｜未投 1｜已投 1｜笔试/测评 1｜面试 1｜Offer 1｜结束 1｜其他 2'
    assert [r['status'] for r in tracker.parse_tracker(md).values()] == states


def test_due_sorted_inclusive_boundaries_invalid_past_and_finished():
    rows = [row('last', deadline='2026-10-21'), row('first', deadline='2026-10-14'),
            row('middle', deadline='2026-10-17'), row('past', deadline='2026-10-13'),
            row('future', deadline='2026-10-22'), row('invalid', deadline='2026-02-30'),
            row('loose', deadline='2026-10-2'), row('done', deadline='2026-10-15', status='结束')]
    md = tracker.render_tracker(rows, NOW)
    assert '即将截止（7 天内）：3 个' in md
    reminders = [line for line in md.splitlines() if line.startswith('- ')]
    assert [line.split('｜')[0] for line in reminders] == ['- 2026-10-14', '- 2026-10-17', '- 2026-10-21']


def test_manual_rows_persist_and_deduplicate_cache():
    manual = [{'company': '手工公司', 'url': 'https://careers.example.test/campus/?source=manual'}]
    original = copy.deepcopy(manual)
    rows = tracker.build_rows(cache(candidate('campus?from=cache')), {}, manual)
    assert len(rows) == 1 and rows[0]['verification'] == '手动'
    assert rows[0]['company'] == '手工公司'
    previous = tracker.parse_tracker(tracker.render_tracker(rows, NOW))
    assert tracker.build_rows({}, previous, []) == rows
    assert manual == original


def test_title_truncated_to_30_characters():
    md = tracker.render_tracker([row(title='招' * 40)], NOW)
    parsed = tracker.parse_tracker(md)
    assert next(iter(parsed.values()))['title'] == '招' * 30


def test_cli_manual_missing_cache_and_rebuild(tmp_path, capsys):
    assert tracker.main(['--add', '公司A', 'https://example.test/jobs/?source=x']) == 0
    md = (tmp_path / 'reports' / '网申进度.md').read_text()
    assert '手动' in md and '未投' in md
    assert '入口 1 个' in capsys.readouterr().out
    assert tracker.main([]) == 0
    assert len(tracker.parse_tracker((tmp_path / 'reports' / '网申进度.md').read_text())) == 1
    assert len(capsys.readouterr().out.splitlines()) == 1
    assert not (tmp_path / 'data' / 'cache' / 'official-cache.json').exists()


def test_cli_cache_and_existing_user_fields(tmp_path):
    (tmp_path / 'data' / 'cache').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'reports').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'data' / 'cache' / 'official-cache.json').write_text(json.dumps(cache(candidate('campus'))))
    old = tracker.render_tracker([row(deadline='2026-10-19', status='已投', note='已完成申请')], NOW)
    (tmp_path / 'reports' / '网申进度.md').write_text(old)
    assert tracker.main([]) == 0
    updated = tracker.parse_tracker((tmp_path / 'reports' / '网申进度.md').read_text())
    assert next(iter(updated.values()))['note'] == '已完成申请'


@pytest.mark.parametrize('content', ['{坏缓存', '[]'])
def test_corrupt_cache_preserves_existing_file(tmp_path, content):
    (tmp_path / 'data' / 'cache').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'reports').mkdir(parents=True, exist_ok=True)
    path = tmp_path / 'reports' / '网申进度.md'
    path.write_text('旧进度')
    (tmp_path / 'data' / 'cache' / 'official-cache.json').write_text(content)
    with pytest.raises(SystemExit) as exc:
        tracker.main([])
    assert exc.value.code == 2 and path.read_text() == '旧进度'


@pytest.mark.parametrize('args', [
    ['--add', '', 'https://example.test'], ['--add', '公司', 'file:///tmp/job'],
    ['--add', '公司', 'https://user:pass@example.test'], ['--add', '公司', 'https://example.test:bad']])
def test_cli_invalid_manual_no_write(tmp_path, args):
    with pytest.raises(SystemExit) as exc:
        tracker.main(args)
    assert exc.value.code == 2 and not (tmp_path / 'reports' / '网申进度.md').exists()


def test_build_rows_drops_aggregator_from_stale_cache():
    from apply_tracker import build_rows
    cache = {'紫讯': {'ts': '2026-10-14T00:00:00+08:00', 'result': {'company': '紫讯', 'jobs': ['AI产品'], 'candidates': [
        {'url': 'https://campus.niuqizp.com/job-x.html', 'title': 'x', 'status': '待核验', 'official': 0.5},
        {'url': 'https://fzzixun.zhiye.com', 'title': '社会招聘', 'status': '已核验', 'official': 0.6}]}}}
    rows = build_rows(cache, {}, [])
    assert [r['url'] for r in rows] == ['https://fzzixun.zhiye.com']
