"""路径与首次写入回归：父目录从不存在开始，不依赖旧文件。"""
import json
from datetime import datetime

import pytest

import shared
from shared import (cache_path, cooldown_path, load_applied_index, load_log,
                    log_path, queue_path, record, report_path, save_log, state_path)


@pytest.mark.parametrize('helper,args,relative', [
    (log_path, ('boss', '深圳'), 'data/logs/boss-深圳-log.json'),
    (cooldown_path, ('liepin',), 'data/cooldown/liepin-cooldown.json'),
    (cache_path, ('jev-cache.json',), 'data/cache/jev-cache.json'),
    (cache_path, ('llm-yupao-cache.json',), 'data/cache/llm-yupao-cache.json'),
    (cache_path, ('official-cache.json',), 'data/cache/official-cache.json'),
    (state_path, ('boss-greet.json',), 'data/boss-greet.json'),
    (state_path, ('runs',), 'data/runs'),
    (state_path, ('evidence',), 'data/evidence'),
    (report_path, ('复核清单.md',), 'reports/复核清单.md'),
    (queue_path, ('51job', '上海'), 'reports/队列/51job-上海-队列.md'),
])
def test_path_helpers_are_pure(tmp_path, helper, args, relative):
    root = tmp_path / '尚不存在'
    assert helper(root, *args) == root / relative
    assert helper(str(root), *args) == root / relative
    assert not root.exists()


def test_save_log_and_record_create_parents(tmp_path):
    path = log_path(tmp_path, 'boss', '深圳')
    log = load_log(path)
    save_log(log, path)
    record(log, path, 'pending', {'job_id': '123'})
    assert json.loads(path.read_text())['pending'] == [{'job_id': '123'}]
    assert list(path.parent.glob('*.tmp')) == []


def test_write_cooldown_creates_parents(tmp_path):
    from apply_runtime import write_cooldown
    path = cooldown_path(tmp_path, 'boss')
    write_cooldown(path, '验证')
    data = json.loads(path.read_text())
    assert data['reason'] == '验证'
    assert datetime.fromisoformat(data['until']) > datetime.now().astimezone()


@pytest.mark.parametrize('kind', ['jev', 'official', 'greet'])
def test_state_writers_create_parents(tmp_path, kind):
    from boss_greet import save_greeted
    from jev_match import _save_cache
    from official_apply import save_cache
    path = state_path(tmp_path, 'boss-greet.json') if kind == 'greet' else cache_path(tmp_path, f'{kind}-cache.json')
    data = {'测试': '状态'}
    if kind == 'jev':
        _save_cache(data, path)
    else:
        (save_greeted if kind == 'greet' else save_cache)(path, data)
    assert json.loads(path.read_text()) == data
    assert list(path.parent.glob('*.tmp')) == []


def test_applied_index_only_scans_data_logs(tmp_path):
    entry = {'job_id': 'root-only'}
    (tmp_path / 'boss-深圳-log.json').write_text(json.dumps({'applied': [entry]}))
    assert load_applied_index(tmp_path) == set()
    path = log_path(tmp_path, 'boss', '深圳')
    save_log({'pending': [{'job_id': 'new-layout'}]}, path)
    nested = path.parent / 'nested' / path.name
    save_log({'applied': [{'job_id': 'nested-only'}]}, nested)
    assert load_applied_index(tmp_path) == {'boss:new-layout'}


def test_review_report_creates_parents_and_ignores_flat_logs(tmp_path, monkeypatch):
    import review_list
    monkeypatch.setattr(shared, 'PROJECT_ROOT', tmp_path)
    old = {'review': [{'job_id': 'legacy', 'job': '旧岗位'}]}
    (tmp_path / 'boss-深圳-log.json').write_text(json.dumps(old))
    assert review_list.load_review_rows(tmp_path) == []
    review_list.main([])
    path = report_path(tmp_path, review_list.REVIEW_FILE)
    assert '# 复核清单' in path.read_text() and '旧岗位' not in path.read_text()


def test_tracker_report_creates_parents(tmp_path, monkeypatch):
    import apply_tracker
    monkeypatch.setattr(shared, 'PROJECT_ROOT', tmp_path)
    assert apply_tracker.main([]) == 0
    assert '# 网申进度' in report_path(tmp_path, '网申进度.md').read_text()


def test_daily_report_and_run_writer_create_parents(tmp_path, monkeypatch):
    import daily
    from types import SimpleNamespace
    plan = {'city': '深圳', 'platforms': {
        p: {'quota': 0, 'keywords': [], 'args': []} for p in daily.PLATFORMS},
        'greet': False, 'inbox': False, 'review_list': False}
    daily.run_daily(plan, root=tmp_path, no_rest=True)
    assert '# 日报' in report_path(tmp_path, '日报.md').read_text()
    assert state_path(tmp_path, 'runs').is_dir()
    # 直接验证子进程输出写函数，不预建日期目录，子进程严格使用替身。
    monkeypatch.setattr(daily.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0))
    path = state_path(tmp_path, 'runs') / '测试日期' / 'boss-1.log'
    assert daily._run(['离线替身'], path, tmp_path) == (0, '')
    assert path.is_file()


def test_default_project_root_ignores_current_directory(tmp_path, monkeypatch):
    """从外部目录加载配置和去重索引时，不能误读 cwd 的同名文件。"""
    root = tmp_path / '项目根'
    elsewhere = tmp_path / '其他工作目录'
    root.mkdir()
    elsewhere.mkdir()
    for directory, city, job_id in ((root, '深圳', 'project-job'),
                                    (elsewhere, '北京', 'cwd-job')):
        (directory / 'config.json').write_text(json.dumps({'city': city}), encoding='utf-8')
        save_log({'pending': [{'job_id': job_id}]}, log_path(directory, 'boss', city))
    monkeypatch.setattr(shared, 'PROJECT_ROOT', root)
    monkeypatch.chdir(elsewhere)
    assert shared.load_config()['city'] == '深圳'
    assert shared.load_applied_index() == {'boss:project-job'}
