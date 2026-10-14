"""复核清单纯函数及离线文件更新。"""
import json

import shared
import review_list as review
from shared import load_applied_index


def row(platform='boss', job_id='a', **kwargs):
    return dict(platform=platform, job_id=job_id, company='公司', job='产品实习',
                url='https://example.com/job', reason='待复核', **kwargs)


def test_parse_approved_only_complete_valid_rows():
    md = review.render_review_list([row(), row('liepin', '123'), row('boss', 'b'),
                                    row('51job', '456')], {'boss:a', 'liepin:123', '51job:456'})
    md = md.replace('[x] | liepin', '[X] | liepin')
    md += '\n| [x] | boss | 缺列 | fake |\n'
    md += '| [x] | unknown | A | B | — | — | — | C | U | a |\n'
    md += '| [x] | boss | A | B | — | — | — | C | U | bad:id |\n'
    md += '| [x] | liepin | A | B | — | — | — | C | U | letters |\n'
    md += '| [x] | 51job | A | B | — | — | — | C | U | 456 |\n'
    assert review.parse_approved(md) == {'boss:a', 'liepin:123'}


def test_collect_latest_cross_log_and_applied_pending(tmp_path):
    (tmp_path / 'data' / 'logs').mkdir(parents=True, exist_ok=True)
    older = row(ts='2026-10-14T10:00:00+08:00')
    newer = {**older, 'ts': '2026-10-14T03:00:00+00:00', 'job': '新标题'}
    logs = {'boss-深圳-log.json': {'review': [newer, older, row(job_id='done'), row(job_id='pending')]},
            'boss-北京-log.json': {'review': [row(job_id='b'), row(job_id='b', ts='invalid')]},
            'liepin-深圳-log.json': {'review': [row('liepin', '1')]},
            '51job-深圳-log.json': {'review': [row('51job', '1')]},
            'yupao-深圳-log.json': {'review': [None, {}, row('yupao', '2')]},
            'boss-old-log.json': {'applied': [row(job_id='done')], 'pending': [row(job_id='pending')]},
            'boss-bad-log.json': {'review': 'invalid'}, 'boss': [], 'other': {}}
    for name, data in logs.items():
        if name.endswith('.json'):
            (tmp_path / 'data' / 'logs' / name).write_text(json.dumps(data))
    rows = review.collect_review_rows(logs, load_applied_index(tmp_path))
    assert {f"{r['platform']}:{r['job_id']}" for r in rows} == {'boss:a', 'boss:b', 'liepin:1', '51job:1', 'yupao:2'}
    assert rows[0]['job'] == '新标题'
    assert len(rows) == 5


def test_render_preserves_checks_sections_sort_metrics_and_51job():
    rows = [row(job_id='old', ts='2026-10-13'),
            row(job_id='new', ts='2026-10-14', jev={'decision': {'hard_ok': .6, 'fit_ev': 2.3, 'action': '1'}}),
            row('51job', '123')]
    md = review.render_review_list(rows, {'boss:old', '51job:123'})
    assert review.parse_approved(md) == {'boss:old'}
    assert md.index('| new |') < md.index('| old |')
    assert '| 0.60 | 2.30 | 复核 |' in md
    assert '| — | 51job |' in md and '51job 暂不支持批准投递，请在页面手动处理；待复核' in md
    assert md.index('## boss') < md.index('## 51job')


def test_cell_content_cannot_inject_approved_row():
    injected = {**row(), 'job': 'A | B\n| [x] | boss | C | D | 1 | 2 | 复核 | E | U | fake |'}
    md = review.render_review_list([injected], set())
    assert not review.parse_approved(md)
    assert '&#124;' in md


def test_regenerate_preserves_old_checks_and_city_filter(tmp_path, monkeypatch):
    (tmp_path / 'data' / 'logs').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'reports').mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(shared, 'PROJECT_ROOT', tmp_path)
    for city, job_id in [('深圳', 'a'), ('北京', 'b')]:
        (tmp_path / 'data' / 'logs' / f'boss-{city}-log.json').write_text(json.dumps({'review': [row(job_id=job_id)]}))
    (tmp_path / 'data' / 'logs' / 'boss-invalid-log.json').write_text('broken')
    path = tmp_path / 'reports' / review.REVIEW_FILE
    path.write_text(review.render_review_list([row()], {'boss:a'}))
    review.main(['--city', '深圳'])
    assert review.parse_approved(path.read_text()) == {'boss:a'}
    assert '| b |' not in path.read_text()
    review.main([])
    assert '| b |' in path.read_text() and review.parse_approved(path.read_text()) == {'boss:a'}
    (tmp_path / 'data' / 'logs' / 'boss-old-log.json').write_text(json.dumps({'pending': [row()]}))
    review.main([])
    assert '| a |' not in path.read_text()


def test_approved_lookup_uses_log_url_only_and_skips_applied(tmp_path):
    (tmp_path / 'data' / 'logs').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'reports').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'data' / 'logs' / 'boss-深圳-log.json').write_text(json.dumps({'review': [row()]}))
    (tmp_path / 'reports' / review.REVIEW_FILE).write_text(
        review.render_review_list([row(), row('liepin', '1'), row(job_id='not-in-log')],
                                  {'boss:a', 'liepin:1', 'boss:not-in-log'}).replace('https://example.com/job', 'https://edited.test'))
    assert review.approved_review_rows(tmp_path, 'boss', '深圳')[0]['url'] == 'https://example.com/job'
    (tmp_path / 'data' / 'logs' / 'boss-other-log.json').write_text(json.dumps({'applied': [row()]}))
    assert not review.approved_review_rows(tmp_path, 'boss', '深圳')


def test_yjs_review_list_approval_regeneration_and_pending_exclusion(tmp_path,monkeypatch):
    (tmp_path / 'data' / 'logs').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'reports').mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(shared, 'PROJECT_ROOT', tmp_path)
    candidate={**row('yingjiesheng','123'),'url':'https://q.yingjiesheng.com/jobdetail/123.html'}
    (tmp_path / 'data' / 'logs' / 'yingjiesheng-深圳-log.json').write_text(json.dumps({'review':[candidate]}))
    path=tmp_path / 'reports' / review.REVIEW_FILE
    path.write_text(review.render_review_list([candidate],{'yingjiesheng:123'}))
    review.main(['--city','深圳'])
    assert review.parse_approved(path.read_text())=={'yingjiesheng:123'}
    assert review.approved_review_rows(tmp_path,'yingjiesheng','深圳')[0]['url']==candidate['url']
    (tmp_path / 'data' / 'logs' / 'yingjiesheng-北京-log.json').write_text(json.dumps({'pending':[candidate]}))
    review.main(['--city','深圳'])
    assert '| 123 |' not in path.read_text()
    assert not review.approved_review_rows(tmp_path,'yingjiesheng','深圳')


def test_manual_form_rows_not_approvable(tmp_path):
    import json
    from review_list import approved_review_rows, render_review_list, REVIEW_FILE
    from shared import log_path, report_path
    row = dict(platform='yingjiesheng', job_id='9', job='产品经理', company='公司A', manual_form=True,
               reason='[需表单]匹配达到推荐阈值', ts='2026-10-15T10:00:00+08:00')
    md = render_review_list([row], {'yingjiesheng:9'})
    assert '| — | yingjiesheng |' in md and '需在平台填写企业表单' in md
    path = log_path(tmp_path, 'yingjiesheng', '深圳'); path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'review': [row]}), encoding='utf-8')
    rp = report_path(tmp_path, REVIEW_FILE); rp.parent.mkdir(parents=True)
    rp.write_text(md.replace('| — | yingjiesheng |', '| [x] | yingjiesheng |'), encoding='utf-8')
    assert approved_review_rows(tmp_path, 'yingjiesheng', '深圳') == []
