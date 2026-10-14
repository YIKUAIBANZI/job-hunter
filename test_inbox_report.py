"""回复日报离线回归：浏览器用严格替身，禁止任何会话交互。"""

import copy
import json
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import inbox_report as report

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=timezone(timedelta(hours=8)))


def item(company='公司A', name='张招聘', time='18:00', status=None, **kwargs):
    return dict(company=company, name=name, time=time, status=status,
                last_msg='请问何时可以到岗？', title='招聘经理', **kwargs)


@pytest.mark.parametrize('value,expected', [
    ({}, 'replied'), ({'status': None}, 'replied'),
    ({'status': '[已读]'}, 'read'), ({'status': '[送达]'}, 'delivered'),
    ({'status': '已送达，已读'}, 'read'), ({'status': ''}, 'unknown'),
    ({'status': '发送失败'}, 'unknown'), ({'status': 1}, 'unknown'),
    (None, 'unknown'), ('异常', 'unknown'),
])
def test_classify(value, expected):
    assert report.classify_boss_item(value) == expected


def test_match_exact_prefix_unmatched_and_only_applied():
    items = [item(' 公 司 A '), item('深圳...'), item('陌生公司'), item(''), item('...')]
    log = {'applied': [dict(company='公司a', job='AI产品实习', ts='2026-10-14T10:00:00'),
                       dict(company='深圳智能科技', job='Agent开发', ts='2026-10-14T11:00:00')],
           'pending': [dict(company='陌生公司', job='未确认岗位')]}
    before = copy.deepcopy((items, log))
    rows = report.match_applied(items, log)
    assert rows[0]['job'] == 'AI产品实习'
    assert rows[0]['applied_at'] == '2026-10-14T10:00:00'
    assert rows[1]['job'] == 'Agent开发'
    assert rows[1]['applied_matches'] == [log['applied'][1]]
    assert rows[0]['source'] == rows[1]['source'] == '本工具投递'
    assert all(r['source'] == '非本工具投递' and not r['job'] and not r['applied_at']
               for r in rows[2:])
    assert (items, log) == before


def test_match_multiple_jobs_preserves_all_and_ignores_bad_rows():
    log = {'applied': [None, {}, dict(company='公司A', job='产品', ts='早'),
                       dict(company='公司A', job='开发', ts='晚'),
                       dict(company='公司A', job='产品', ts='早')]}
    row = report.match_applied([item()], log)[0]
    assert row['job'] == '产品 / 开发' and row['applied_at'] == '早 / 晚'
    assert len(row['applied_matches']) == 3
    assert report.match_applied([item('司A')], log)[0]['source'] == '非本工具投递'
    assert report.match_applied([item()], {})[0]['source'] == '非本工具投递'


def test_dedup_snapshot_latest_and_identity():
    first = item(status='[送达]')
    changed = item(status='[已读]')
    different_name = item(name='李招聘')
    different_time = item(time='昨天')
    different_company = item(company='公司B')
    rows = [first, different_name, different_time, different_company, changed]
    before = copy.deepcopy(rows)
    result = report.dedup_boss_items(rows)
    assert result == [changed, different_name, different_time, different_company]
    assert rows == before


def test_render_tables_counts_escape_and_truncate():
    replied = {**item('公|司\nA', name='张\r\n招聘'), 'job': '产|品',
               'last_msg': '|\n<img> [链接](evil) `代码` \\ ' + '文' * 70}
    read = {**item('公司B', status='[已读]'), 'job': '开发'}
    delivered = item('不列送达公司', status='[送达]')
    rows = [replied, read, delivered, item('未知公司', status='异常')]
    before = copy.deepcopy(rows)
    md = report.render_report(rows, 3, NOW)
    assert '生成时间：2026-10-14 18:00:00+08:00' in md
    assert 'Boss：会话 4｜HR 已回复 1｜已读未回 1｜仅送达 1' in md
    assert '猎聘未读 3（请在猎聘网页手动查看）' in md
    assert '状态未知：1' in md
    replied_table, read_table = md.split('## 已读未回')
    assert '| 公司 | 岗位 | 招聘者 | 时间 | 最后一条消息 |' in replied_table
    assert '| 公&#124;司 A | 产&#124;品 | 张 招聘 | 18:00 |' in replied_table
    assert '&lt;img&gt;' in md and '\\[链接\\]' in md and '\\`代码\\`' in md
    assert '<img>' not in md and '\r' not in md
    assert '文' * 60 not in md and '…' in md
    assert '| 公司 | 岗位 | 时间 |' in read_table
    assert '| 公司B | 开发 | 18:00 |' in read_table
    assert '不列送达公司' not in md and '仅送达：1 个会话' in md
    assert rows == before


def test_render_empty_and_unmatched():
    md = report.render_report([], 0, NOW)
    assert md.count('暂无') == 2
    assert '会话 0｜HR 已回复 0｜已读未回 0｜仅送达 0' in md
    assert '猎聘未读 0' in md
    rows = report.match_applied([item()], {})
    assert '| 公司A | 非本工具投递 |' in report.render_report(rows, None, NOW)
    assert '猎聘未读 未读取（--no-liepin）' in report.render_report([], None, NOW)


@pytest.fixture
def offline(monkeypatch, tmp_path):
    import apply_runtime
    import boss_apply
    import shared

    def forbidden(*args, **kwargs):
        pytest.fail('离线测试禁止真实连接与会话交互')

    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(report, '__file__', str(tmp_path / 'inbox_report.py'))
    sleeps, checks, tabs = [], [], []
    monkeypatch.setattr(report.time, 'sleep', sleeps.append)
    monkeypatch.setattr(report.random, 'uniform', lambda a, b: a)
    state = dict(block_phase='', nav_error=False, unread='4', bad_snapshot=False,
                 close_error=False)

    class Tab:
        url = ''
        closed = False
        phase = 'new'
        scrolls = 0
        click = input = eles = ele = forbidden

        def get(self, url):
            self.url = url
            self.phase = 'navigated'
            if state['nav_error']:
                raise RuntimeError('导航断连')

        def run_js(self, script):
            if script == report.BOSS_ITEMS_JS:
                self.phase = 'boss_read'
                if state['bad_snapshot']:
                    return None
                # 模拟窗口化懒加载：前面会话后续从 DOM 消失，但汇总仍保留。
                return [item(company=f'公司{self.scrolls}'), item(company='重复')]
            if script == report.BOSS_SCROLL_JS:
                self.phase = 'scroll'
                self.scrolls += 1
                return True
            if script == report.LIEPIN_UNREAD_JS:
                self.phase = 'liepin_read'
                return state['unread']
            forbidden()

        def close(self):
            self.closed = True
            if state['close_error']:
                raise RuntimeError('关闭断连')

    def new_tab():
        tab = Tab()
        tabs.append(tab)
        return tab

    connections = []
    monkeypatch.setattr(shared, 'connect_chrome',
                        lambda: connections.append('connect') or SimpleNamespace(new_tab=new_tab))

    def block(tab):
        checks.append((tab.url, tab.phase))
        return '验证码' if tab.phase == state['block_phase'] else ''

    monkeypatch.setattr(boss_apply, 'page_block_reason', block)
    path = tmp_path / report.REPORT_FILE
    path.write_text('上次日报', encoding='utf-8')
    (tmp_path / 'boss-深圳-log.json').write_text(json.dumps({'applied': [
        dict(company='公司0', job='产品实习', ts=NOW.isoformat())]}), encoding='utf-8')
    return SimpleNamespace(state=state, tabs=tabs, sleeps=sleeps, checks=checks,
                           root=tmp_path, path=path, connections=connections,
                           write_cooldown=apply_runtime.write_cooldown)


@pytest.mark.parametrize('no_liepin', [False, True])
def test_main_readonly_bounds_and_own_tab_cleanup(offline, no_liepin, capsys):
    h = offline
    assert report.main(['--no-liepin'] if no_liepin else []) == 0
    assert len(h.tabs) == (1 if no_liepin else 2)
    assert all(tab.closed for tab in h.tabs)
    assert h.tabs[0].url == report.BOSS_URL and h.tabs[0].scrolls == 5
    assert h.sleeps == [5] + [1.5] * 5 + ([] if no_liepin else [5])
    md = h.path.read_text(encoding='utf-8')
    assert '会话 7｜HR 已回复 7' in md and '| 公司0 | 产品实习 |' in md
    assert ('猎聘未读 未读取' if no_liepin else '猎聘未读 4') in md
    assert '已更新｜Boss 会话 7' in capsys.readouterr().out
    # 一个快照必须被前后两次风控检查夹住。
    assert sum(phase == 'boss_read' for _, phase in h.checks) >= 6


@pytest.mark.parametrize('phase', ['navigated', 'boss_read', 'scroll', 'liepin_read'])
def test_block_no_report_or_log_writes_and_cleanup(offline, phase, capsys):
    h = offline
    before = {p.name: p.read_bytes() for p in h.root.iterdir()}
    h.state['block_phase'] = phase
    assert report.main([]) == 1
    assert all(tab.closed for tab in h.tabs)
    assert {p.name for p in h.root.iterdir()} == set(before) | {'boss-cooldown.json'}
    assert all((h.root / name).read_bytes() == data for name, data in before.items())
    data = json.loads((h.root / 'boss-cooldown.json').read_text())
    assert data['reason'] == '验证码'
    remaining = datetime.fromisoformat(data['until']) - datetime.now().astimezone()
    assert timedelta(hours=1, minutes=59) < remaining <= timedelta(hours=2)
    assert '风控：验证码' in capsys.readouterr().out


def test_cooldown_no_connect_or_writes(offline):
    h = offline
    h.write_cooldown(h.root / 'boss-cooldown.json', '已有风控')
    before = {p.name: p.read_bytes() for p in h.root.iterdir()}
    assert report.main([]) == 1 and not h.connections
    assert all((h.root / name).read_bytes() == data for name, data in before.items())


@pytest.mark.parametrize('kind', ['nav_error', 'bad_snapshot'])
def test_read_failure_preserves_report_and_closes(offline, kind):
    offline.state[kind] = True
    assert report.main([]) == 1
    assert offline.path.read_text() == '上次日报'
    assert all(tab.closed for tab in offline.tabs)


@pytest.mark.parametrize('raw,expected', [(None, 0), ('0', 0), (' 12 ', 12)])
def test_liepin_count_absent_and_integer(offline, raw, expected):
    offline.state['unread'] = raw
    assert report.main([]) == 0
    assert f'猎聘未读 {expected}' in offline.path.read_text()


@pytest.mark.parametrize('raw', ['', '99+', '-1', '异常'])
def test_liepin_bad_count_preserves_report(offline, raw):
    offline.state['unread'] = raw
    assert report.main([]) == 1
    assert offline.path.read_text() == '上次日报'
    assert all(tab.closed for tab in offline.tabs)


def test_no_scroll_container_and_empty_snapshot(monkeypatch):
    calls, checks = [], []

    def run_js(script):
        calls.append(script)
        return [] if script == report.BOSS_ITEMS_JS else False

    assert report.read_boss_items(SimpleNamespace(run_js=run_js), checks.append) == []
    assert calls == [report.BOSS_ITEMS_JS, report.BOSS_SCROLL_JS]
    assert len(checks) == 4


@pytest.mark.parametrize('block_at', [1, 2])
def test_snapshot_block_checks_before_and_after(block_at):
    events = []
    checked = 0

    def check(tab):
        nonlocal checked
        checked += 1
        events.append('check')
        if checked == block_at:
            raise report.PageBlocked('验证码')

    def run_js(script):
        assert script == report.BOSS_ITEMS_JS
        events.append('read')
        return [item()]

    with pytest.raises(report.PageBlocked):
        report.read_boss_items(SimpleNamespace(run_js=run_js), check)
    assert events == (['check'] if block_at == 1 else ['check', 'read', 'check'])


def test_cooldown_write_failure_still_stops(offline, monkeypatch, capsys):
    import apply_runtime

    def cannot_write(*args):
        raise OSError('只读目录')

    monkeypatch.setattr(apply_runtime, 'write_cooldown', cannot_write)
    offline.state['block_phase'] = 'boss_read'
    assert report.main([]) == 1
    assert offline.path.read_text() == '上次日报'
    assert all(tab.closed for tab in offline.tabs)
    assert '冷却文件写入失败' in capsys.readouterr().out


def test_tab_cleanup_continues_after_one_close_failure(offline, capsys):
    offline.state['close_error'] = True
    assert report.main([]) == 0
    assert len(offline.tabs) == 2 and all(tab.closed for tab in offline.tabs)
    assert capsys.readouterr().out.count('自建标签关闭失败') == 2


def test_help_exits_before_connection(offline, capsys):
    with pytest.raises(SystemExit) as exc:
        report.main(['--help'])
    assert exc.value.code == 0 and not offline.connections
    assert '--no-liepin' in capsys.readouterr().out


def test_system_resume_message_not_counted_as_reply():
    from inbox_report import classify_boss_item
    item = {'status': None, 'last_msg': '您的附件简历 Banz_AI_P... 已发送给Boss点击查看附件'}
    assert classify_boss_item(item) == 'delivered'
    assert classify_boss_item({'status': None, 'last_msg': '收到感谢投递'}) == 'replied'
