import json
import socket
from types import SimpleNamespace

import pytest

import apply_runtime as rt
import platform_runner as runner
import platform_adapters as adapters
from platform_adapters import build_search_url, parse_job_id, same_job
from shared import load_applied_index, load_log, load_config


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*a, **kw): pytest.fail('禁止真实网络或浏览器连接')
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(runner, 'connect_chrome', blocked)
    monkeypatch.setattr(runner.jev.request, 'urlopen', blocked)


@pytest.mark.parametrize('platform,url,expected', [
    ('liepin','https://www.liepin.com/job/1986026009.shtml?x=1','1986026009'),
    ('liepin','https://www.liepin.com/a/79805647.shtml','79805647'),
    ('51job','https://jobs.51job.com/shenzhen/171684906.html','171684906'),
    ('51job','https://jobs.51job.com/all/coABC.html',''),
    ('yupao','/zhaogong/426249613/AoTgiuIF.html','426249613'),
    ('yupao','/zhaogong/426249613.html','426249613'),
    ('yupao','/zhaogong/a77c0/',''),
    ('yupao','https://evil.test/zhaogong/123.html',''),
    ('liepin','https://liepin.com.evil.test/job/123.shtml',''),
    ('liepin','javascript:/job/123.shtml',''),
])
def test_ids(platform,url,expected):
    assert parse_job_id(platform,url)==expected


@pytest.mark.parametrize('platform',['liepin','51job','yupao'])
def test_search_query_encoded(platform):
    url=build_search_url(platform,'AI & 产品?#','123')
    assert 'AI+%26+%E4%BA%A7%E5%93%81%3F%23' in url
    assert '#' not in url


def test_same_title_different_id_rejected():
    assert not same_job({'title':'产品经理','job_id':'1'},{'title':'产品经理','job_id':'2'})
    assert not same_job({'title':'产品经理','job_id':'1'},{'title':'销售','job_id':'1'})
    assert not same_job({'title':'','job_id':'1'},{'title':'','job_id':'1'})


def test_log_namespace_and_legacy_ids(tmp_path):
    (tmp_path / 'data' / 'logs').mkdir(parents=True, exist_ok=True)
    for platform in ('51job','liepin','yupao','boss'):
        (tmp_path / 'data' / 'logs' / f'{platform}-old-log.json').write_text(json.dumps({'pending':[{'jobId':'123','company':'A','job':'B'}]}))
    index=load_applied_index(tmp_path)
    assert {f'{p}:123' for p in ('51job','liepin','yupao','boss')} <= index
    assert 'a||b' in index


def test_no_false_success_from_unrelated_zero_code():
    info={'url':'https://www.liepin.com/analytics','http_status':200,'code':0,'correlated':True}
    assert rt.classify_response(info,{})=='pending'
    info.update(url='https://www.liepin.com/submit',correlated=False)
    assert rt.classify_response(info,{info['url']:0})=='pending'
    info['correlated']=True
    assert rt.classify_response(info,{info['url']:0})=='applied'
    info['blocked']=True
    assert rt.classify_response(info,{info['url']:0})=='blocked'


def test_request_correlation_not_substring():
    assert rt.request_job_ids('jobId=123&token=123456')=={'123'}
    assert rt.request_job_ids({'description':'jobId=123'})==set()
    assert rt.request_job_ids({'data':{'jobIds':['123','456']}})=={'123','456'}


class Button:
    def __init__(self, events, explode=False): self.events,self.explode=events,explode
    def click(self,**kw):
        self.events.append('click')
        if self.explode: raise RuntimeError('disconnected')


class Listener:
    def __init__(self,events,packets=()): self.events,self.packets=events,list(packets)
    def start(self,*args,**kw): self.events.append('listen')
    def wait(self,**kw): return self.packets.pop(0) if self.packets else None
    def stop(self): self.events.append('stop')


class SubmitAdapter:
    domain='liepin.com'
    success_texts=('继续聊',)
    verified_endpoints={}
    def __init__(self, events, *, explode=False, match=True, ui=True):
        self.events,self.match,self.ui=events,match,ui
        self.b=Button(events,explode)
        self.action_tab=SimpleNamespace(listen=Listener(events))
    def verified_button(self,c): return self.b if self.match else None
    def button_state(self,c): return '继续聊' if 'click' in self.events and self.ui else '聊一聊'


@pytest.fixture
def checks(monkeypatch):
    monkeypatch.setattr(rt,'page_block_reason',lambda t:'')
    monkeypatch.setattr(rt,'page_gate_reason',lambda t:'')


def test_dry_run_no_listener_no_click(checks):
    events=[]
    result=rt.submit(SubmitAdapter(events),{'job_id':'1'},lambda:events.append('persist'),dry_run=True)
    assert result[0]=='skipped' and events==[]


def test_identity_failure_never_clicks(checks):
    events=[]
    result=rt.submit(SubmitAdapter(events,match=False),{'job_id':'1'},lambda:events.append('persist'))
    assert result[0]=='review' and events==[]


def test_persist_before_click_disconnect_pending(checks):
    events=[]
    result=rt.submit(SubmitAdapter(events,explode=True),{'job_id':'1'},lambda:events.append('persist'))
    assert result[0]=='pending'
    assert events==['listen','persist','click','stop']


def test_persist_failure_no_click(checks):
    events=[]
    def fail(): raise OSError('disk full')
    result=rt.submit(SubmitAdapter(events),{'job_id':'1'},fail)
    assert result[0]=='review' and 'click' not in events


def test_ui_confirmation_requires_change(checks):
    events=[]
    result=rt.submit(SubmitAdapter(events),{'job_id':'1'},lambda:events.append('persist'),timeout=.002)
    assert result[0]=='applied'
    events=[]
    result=rt.submit(SubmitAdapter(events,ui=False),{'job_id':'1'},lambda:events.append('persist'),timeout=.002)
    assert result[0]=='pending'


def test_block_beats_click(checks,monkeypatch):
    monkeypatch.setattr(rt,'page_block_reason',lambda t:'验证')
    events=[]
    assert rt.submit(SubmitAdapter(events),{'job_id':'1'},lambda:None)[0]=='blocked'
    assert not events


class FakeAdapter:
    calibrated=True
    def __init__(self,cards=None,block=False,bad_detail=False):
        self.items=cards or [dict(job_id=str(i),title='产品实习',company=f'公司{i}',salary='',url=f'https://example.com/{i}') for i in range(1,5)]
        self.opened=[]; self.searches=0; self.closed=0; self.block=block; self.bad_detail=bad_detail
    def search(self): self.searches+=1
    def check(self): return ('blocked','验证') if self.block else ('','')
    def cards(self): return self.items
    def open_detail(self,card):
        self.opened.append(card['job_id'])
        return {**card,'job_id':'WRONG' if self.bad_detail else card['job_id'],'jd':'产品 实习 AI'}
    def close_detail(self): self.closed+=1
    def next_page(self,n): return False
    def close(self): pass


def args(*extra):
    return runner.parse_args({'深圳':'123'},['--job','产品','--city','深圳','--count','2','--matcher','keyword',*extra])


def config(tmp_path):
    cfg=load_config(tmp_path);cfg.update(target_roles=['产品'],min_score=0)
    return cfg


def run(tmp_path,adapter,a=None):
    return runner.execute('liepin',a or args('--dry-run'),config(tmp_path),adapter,tmp_path,rest=lambda *a:None)


def test_dry_run_main_zero_submits_and_count(tmp_path,monkeypatch):
    monkeypatch.setattr(runner,'submit',lambda *a,**k:pytest.fail('dry-run submitted'))
    adapter=FakeAdapter();stats=run(tmp_path,adapter)
    assert stats['evaluated']==2 and adapter.opened==['1','2']
    log=load_log(tmp_path / 'data' / 'logs' / 'liepin-深圳-log.json')
    assert not log['applied'] and not log['pending']


def test_mismatched_detail_review_no_submit(tmp_path,monkeypatch):
    monkeypatch.setattr(runner,'submit',lambda *a,**k:pytest.fail('wrong job submitted'))
    stats=run(tmp_path,FakeAdapter(bad_detail=True),args())
    assert stats['review']==4 and not stats['applied']


def test_pending_stops_and_next_run_dedup(tmp_path,monkeypatch):
    def pending(a,c,pre,**kw): pre();return 'pending','未知',{'attempted':True}
    monkeypatch.setattr(runner,'submit',pending)
    stats=run(tmp_path,FakeAdapter(),args())
    assert stats['pending']==1 and stats['views']==1
    adapter=FakeAdapter();run(tmp_path,adapter)
    assert '1' not in adapter.opened


def test_count_stops_after_success(tmp_path,monkeypatch):
    def applied(a,c,pre,**kw): pre();return 'applied','确认',{'attempted':True}
    monkeypatch.setattr(runner,'submit',applied)
    adapter=FakeAdapter();stats=run(tmp_path,adapter,args())
    assert stats['applied']==2 and adapter.opened==['1','2']
    assert not load_log(tmp_path / 'data' / 'logs' / 'liepin-深圳-log.json')['pending']


def test_block_writes_cooldown_before_next_run(tmp_path):
    stats=run(tmp_path,FakeAdapter(block=True))
    assert '验证' in stats['stop_reason']
    assert (tmp_path / 'data' / 'cooldown' / 'liepin-cooldown.json').exists()
    adapter=FakeAdapter();run(tmp_path,adapter)
    assert adapter.searches==0
    assert load_log(tmp_path / 'data' / 'logs' / 'liepin-深圳-log.json')['failed'][0]['status']=='blocked'


def test_max_views(tmp_path):
    adapter=FakeAdapter();stats=run(tmp_path,adapter,args('--dry-run','--max-views','1'))
    assert stats['views']==1 and len(adapter.opened)==1


def test_uncalibrated_no_browser(tmp_path):
    adapter=FakeAdapter();adapter.calibrated=False
    stats=run(tmp_path,adapter,args())
    assert adapter.searches==0 and '校准' in stats['stop_reason']


def test_jev_failure_stops_three(tmp_path,monkeypatch):
    cfg=config(tmp_path);cfg['resume_path']='resume.txt';(tmp_path/'resume.txt').write_text('非敏感测试简历')
    monkeypatch.setattr(runner.jev,'load_jev_key',lambda:'test-key')
    def fail(*a,**k): raise runner.jev.JevError('failed')
    monkeypatch.setattr(runner.jev,'match_title',fail)
    monkeypatch.setattr(runner.jev,'match_job',fail)  # 实习标题受保护，可能直接进入 JD 匹配
    a=args('--dry-run','--count','5','--matcher','jev')
    stats=runner.execute('liepin',a,cfg,FakeAdapter(),tmp_path,rest=lambda *a:None)
    assert stats['review']==3 and '3' in stats['stop_reason']


def test_cache_context_changes():
    cfg={'exclude_keywords':[]}
    assert runner.cache_scope('51job',cfg,'a','x') != runner.cache_scope('51job',cfg,'b','x')
    assert runner.cache_scope('51job',cfg,'a','x') != runner.cache_scope('liepin',cfg,'a','x')


def test_jev_scope_exact_legacy_payload_and_llm_isolation():
    import hashlib
    jev = runner.jev
    cfg = {'exclude_keywords': ['总监']}
    try:
        jev.configure({})
        # 改动前原始算法；不得给 Jev 增加空后端字段。
        payload = json.dumps(['简历', '偏好', cfg.get('exclude_keywords'),
                              jev.QUESTIONS, jev.TITLE_QUESTIONS, jev.JEV_MODEL,
                              [jev.HARD_SKIP_THRESHOLD, jev.HARD_APPLY_THRESHOLD, jev.FIT_SKIP_THRESHOLD,
                               jev.FIT_APPLY_THRESHOLD, jev.ACTION_THRESHOLD, jev.TITLE_SKIP_THRESHOLD],
                              'multi-platform-v1'], ensure_ascii=False, sort_keys=True)
        expected = 'liepin:' + hashlib.sha256(payload.encode()).hexdigest()[:16]
        assert runner.cache_scope('liepin', cfg, '简历', '偏好') == expected
        jev.configure({'matcher': 'llm', 'llm': {'base_url': 'https://example.com/v1', 'model': 'cheap'}})
        first = runner.cache_scope('liepin', cfg, '简历', '偏好')
        assert first != expected
        jev.configure({'matcher': 'llm', 'llm': {'base_url': 'https://other.com/v1', 'model': 'cheap'}})
        assert runner.cache_scope('liepin', cfg, '简历', '偏好') != first
        jev.configure({'matcher': 'llm', 'llm': {'base_url': 'https://example.com/v1', 'model': 'other'}})
        assert runner.cache_scope('liepin', cfg, '简历', '偏好') != first
    finally:
        jev.configure({})


@pytest.mark.parametrize('platform', ['liepin', 'yingjiesheng', '51job', 'yupao'])
def test_main_reads_city_and_matcher_defaults(monkeypatch, platform):
    cfg = {'default_count': 2, 'min_score': 60, 'city': '杭州', 'matcher': 'llm'}
    monkeypatch.setattr(runner, 'load_config', lambda: cfg)
    def execute(p, a, config, adapter, root):
        assert a.city == '杭州' and a.matcher == 'llm'
        assert runner.jev.BACKEND == 'llm' and adapter.browser is None
        return {'views': 0}
    monkeypatch.setattr(runner, 'execute', execute)
    try:
        assert runner.main(platform, {'深圳': '123', '杭州': '456'}, ['--job', '运营']) == {'views': 0}
    finally:
        runner.jev.configure({})


@pytest.mark.parametrize('missing', ['key', 'base_url', 'model'])
def test_llm_missing_configuration_exits_without_browser(tmp_path, monkeypatch, missing, capsys):
    cfg = config(tmp_path)
    cfg['llm'].update(base_url='https://example.com/v1', model='cheap')
    if missing != 'key':
        cfg['llm'][missing] = ''
    monkeypatch.setattr(runner.llm_match, 'load_llm_key', lambda *a: '' if missing == 'key' else 'offline-key')
    adapter = FakeAdapter()
    try:
        stats = runner.execute('liepin', args('--dry-run', '--matcher', 'llm'), cfg, adapter, tmp_path, rest=lambda *a: None)
        assert adapter.searches == 0 and 'config_tool.py check' in stats['stop_reason']
    finally:
        runner.jev.configure({})


def test_llm_runner_reuses_review_gate_cache_and_three_failure_stop(tmp_path, monkeypatch):
    (tmp_path / 'data' / 'cache').mkdir(parents=True, exist_ok=True)
    from test_llm_match import full_answers, response
    cfg = config(tmp_path)
    cfg.update(resume_path='resume.txt', resume_redact=['张三'])
    cfg['llm'].update(base_url='https://example.com/v1', model='cheap')
    (tmp_path/'resume.txt').write_text('张三 13812345678\n运营与产品项目')
    monkeypatch.setattr(runner.llm_match, 'load_llm_key', lambda *a: 'offline-key')
    requests = []
    raw = full_answers()
    raw['action'].update(choice='1', probabilities={'0': 0, '1': 1, '2': 0})
    def urlopen(req, **kw):
        requests.append(req)
        assert '张三' not in req.data.decode() and '13812345678' not in req.data.decode()
        return response(json.dumps(raw))
    monkeypatch.setattr(runner.jev.request, 'urlopen', urlopen)
    try:
        stats = runner.execute('liepin', args('--dry-run', '--matcher', 'llm'), cfg, FakeAdapter(), tmp_path, rest=lambda *a: None)
        assert stats['review'] == 2 and not stats['applied'] and len(requests) == 2
        assert (tmp_path / 'data' / 'cache' / 'llm-liepin-cache.json').exists() and not (tmp_path / 'data' / 'cache' / 'jev-liepin-cache.json').exists()
        runner.execute('liepin', args('--dry-run', '--matcher', 'llm'), cfg, FakeAdapter(), tmp_path, rest=lambda *a: None)
        assert len(requests) == 2
        monkeypatch.setattr(runner.jev.request, 'urlopen', lambda *a, **kw: response('{}'))
        stats = runner.execute('liepin', args('--dry-run', '--matcher', 'llm', '--refresh', '--count', '5'), cfg,
                               FakeAdapter(), tmp_path, rest=lambda *a: None)
        assert stats['review'] == 3 and '3' in stats['stop_reason']
    finally:
        runner.jev.configure({})


def test_review_policy_preserves_raw_jev_result():
    result={'decision':{'decision':'apply','action':'1','reason':'方案B'}}
    assert runner.effective_decision(result,'review-gate')['decision']=='review'
    assert runner.effective_decision(result,'boss-b')['decision']=='apply'
    assert result['decision']['decision']=='apply'


def test_cached_review_does_not_open_detail(tmp_path,monkeypatch):
    cfg=config(tmp_path);cfg['resume_path']='resume.txt';(tmp_path/'resume.txt').write_text('测试简历')
    monkeypatch.setattr(runner.jev,'load_jev_key',lambda:'test-key')
    monkeypatch.setattr(runner.jev,'latest_match',lambda *a,**k:{'decision':{'decision':'review','reason':'待确认'}})
    monkeypatch.setattr(runner.jev,'match_title',lambda *a,**k:pytest.fail('cached title called'))
    adapter=FakeAdapter()
    stats=runner.execute('liepin',args('--dry-run','--matcher','jev'),cfg,adapter,tmp_path,rest=lambda *a:None)
    assert stats['review']==2 and not adapter.opened


def test_review_gate_cached_apply_does_not_open_for_live(tmp_path,monkeypatch):
    cfg=config(tmp_path);cfg['resume_path']='resume.txt';(tmp_path/'resume.txt').write_text('测试简历')
    monkeypatch.setattr(runner.jev,'load_jev_key',lambda:'test-key')
    monkeypatch.setattr(runner.jev,'latest_match',lambda *a,**k:{'decision':{'decision':'apply','action':'1','reason':'B'}})
    adapter=FakeAdapter()
    stats=runner.execute('liepin',args('--matcher','jev'),cfg,adapter,tmp_path,rest=lambda *a:None)
    assert stats['review']==4 and not adapter.opened


def test_opener_ownership_not_any_new_tab():
    from platform_adapters import PlatformAdapter
    a=PlatformAdapter('liepin',None,SimpleNamespace(),'123')
    a.search_tab=SimpleNamespace(tab_id='search')
    own=SimpleNamespace(tab_id='own',run_cdp=lambda *a,**k:{'targetInfo':{'openerId':'search'}})
    other=SimpleNamespace(tab_id='other',run_cdp=lambda *a,**k:{'targetInfo':{'openerId':'another-chat'}})
    assert a.opened_by_search(own)
    assert not a.opened_by_search(other)


def test_packet_evidence_redacts_secrets_and_boolean_code():
    packet=SimpleNamespace(url='https://www.liepin.com/submit?jobId=123&token=SECRET',
                           response=SimpleNamespace(body={'code':False,'message':'account@example.com'},status=200),
                           request=SimpleNamespace(postData={'jobId':'123','secret':'PRIVATE'}))
    info=rt.packet_info(packet,'123')
    assert info['correlated'] and info['code'] is None
    assert not any(s in json.dumps(info) for s in ('SECRET','PRIVATE','example.com'))


def test_block_during_post_click_exception(checks,monkeypatch):
    events=[]
    monkeypatch.setattr(rt,'page_block_reason',lambda t:'验证' if 'click' in events else '')
    result=rt.submit(SubmitAdapter(events,explode=True),{'job_id':'1'},lambda:events.append('persist'))
    assert result[0]=='blocked' and result[2]['attempted']


def test_post_click_process_error_preserves_prewrite(tmp_path,monkeypatch):
    def crash(a,c,pre,**kw): pre();raise RuntimeError('crash')
    monkeypatch.setattr(runner,'submit',crash)
    run(tmp_path,FakeAdapter(),args())
    log=load_log(tmp_path / 'data' / 'logs' / 'liepin-深圳-log.json')
    assert len(log['pending'])==1 and 'liepin:1' in load_applied_index(tmp_path)


def test_external_campus_job_skipped_without_submit(tmp_path,monkeypatch):
    adapter=FakeAdapter()
    adapter.open_detail=lambda c:{'external':True}
    monkeypatch.setattr(runner,'submit',lambda *a,**k:pytest.fail('external job submitted'))
    stats=run(tmp_path,adapter,args())
    assert stats['skip']==4 and stats['applied']==0


def test_pending_summary_includes_crash_after_prewrite(tmp_path,monkeypatch):
    def crash(a,c,pre,**kw): pre();raise RuntimeError('crash')
    monkeypatch.setattr(runner,'submit',crash)
    stats=run(tmp_path,FakeAdapter(),args())
    assert stats['pending']==1


def test_selected_job_never_submits_other_card(tmp_path,monkeypatch):
    submitted=[]
    def applied(a,c,pre,**kw): submitted.append(c['job_id']);pre();return 'applied','确认',{'attempted':True}
    monkeypatch.setattr(runner,'submit',applied)
    stats=run(tmp_path,FakeAdapter(),args('--job-id','3','--count','1','--calibrate'))
    assert stats['applied']==1 and submitted==['3']


def test_missing_selected_job_never_submits(tmp_path,monkeypatch):
    monkeypatch.setattr(runner,'submit',lambda *a,**k:pytest.fail('unapproved job submitted'))
    stats=run(tmp_path,FakeAdapter(),args('--job-id','99','--count','1','--calibrate'))
    assert stats['applied']==0 and stats['views']==0


@pytest.mark.parametrize('platform',['liepin','yupao'])
def test_browser_button_args_supported_by_drission(platform):
    from DrissionPage._elements.chromium_element import convert_argument
    from platform_adapters import PlatformAdapter
    a=PlatformAdapter(platform,None,SimpleNamespace(),'123')
    card={'job_id':'1','title':'产品经理'}
    a.detail_snapshot=lambda:card
    marker=object()
    def js(script,*values):
        for value in values: convert_argument(value)
        return marker
    a.search_tab=a.detail_tab=SimpleNamespace(run_js=js)
    assert a._button(card) is marker


def test_unknown_business_envelope_retained_but_not_accepted():
    packet=SimpleNamespace(url='https://api-c.liepin.com/api/open-chat',
                           response=SimpleNamespace(body={'flag':1,'data':{'private':'DO_NOT_LOG'},'message':'PRIVATE'},status=200),
                           request=SimpleNamespace(postData={'jobId':'85442155','userId':'DO_NOT_LOG'}))
    info=rt.packet_info(packet,'1912345601')
    assert info['business_flags']=={'flag':1}
    assert info['body_fields']==['data','flag','message']
    assert info['request_job_ids']==['85442155'] and not info['correlated']
    assert rt.classify_response(info,{})=='pending'
    assert 'DO_NOT_LOG' not in json.dumps(info) and 'PRIVATE' not in json.dumps(info)


def test_other_host_urls_recorded_without_query(checks):
    events=[]
    adapter=SubmitAdapter(events)
    pkt=SimpleNamespace(url='https://api.other.cn/apply/send?token=secret&jobId=1')
    adapter.action_tab=SimpleNamespace(listen=Listener(events,[pkt,pkt]))
    result=rt.submit(adapter,{'job_id':'1'},lambda:events.append('persist'),timeout=.05)
    assert result[2]['other_urls']==['api.other.cn/apply/send']
    assert result[2]['packets']==[]


def test_liepin_open_chat_flag_alias_correlates_and_applies():
    url='https://api-c.liepin.com/api/com.liepin.im.c.chat.open-chat'
    pkt=SimpleNamespace(url=url,request=SimpleNamespace(postData='{"jobId":"12345678"}'),
                        response=SimpleNamespace(status=200,body={'flag':1,'data':{}}))
    adapter=adapters.PlatformAdapter.__new__(adapters.PlatformAdapter); adapter.platform='liepin'
    info=rt.packet_info(pkt,'1912345678',adapter.request_id_aliases('1912345678'))
    assert info['correlated']
    assert rt.classify_response(info,adapters.VERIFIED_ENDPOINTS['liepin'])=='applied'
    bad={**info,'business_flags':{'flag':0}}
    assert rt.classify_response(bad,adapters.VERIFIED_ENDPOINTS['liepin'])=='failed'
    assert rt.packet_info(pkt,'1912345678')['correlated'] is False  # 无别名不关联


def test_calibrated_platforms_and_alias_scoped():
    a=adapters.PlatformAdapter.__new__(adapters.PlatformAdapter)
    a.platform='yupao'
    assert a.request_id_aliases('1912345678')==set()
    assert adapters.CALIBRATED=={'liepin','yingjiesheng','51job','yupao'}


@pytest.mark.parametrize('url,expected', [
    ('https://q.yingjiesheng.com/jobdetail/123.html', '123'),
    ('https://q.yingjiesheng.com/jobdetail/123.html?jobid=123', '123'),
    ('https://yingjiesheng.com/jobdetail/123.html', '123'),
    ('https://young.yingjiesheng.com/pc/jobdetail?jobid=123', ''),
    ('https://yingjiesheng.com.evil.test/jobdetail/123.html', ''),
    ('https://evil.test/jobdetail/123.html', ''),
    ('https://q.yingjiesheng.com/jobdetail/x.html', ''),
])
def test_yjs_job_ids(url, expected):
    assert parse_job_id('yingjiesheng', url) == expected


def test_yjs_search_url_and_cities():
    import importlib
    entry = importlib.import_module('yingjiesheng_apply')
    old = importlib.import_module('51job_apply')
    assert entry.CITY_CODES == old.CITY_CODES
    url = build_search_url('yingjiesheng', '产品经理 / AI?#', '040000')
    assert url == 'https://q.yingjiesheng.com/jobs/search/%E4%BA%A7%E5%93%81%E7%BB%8F%E7%90%86%20%2F%20AI%3F%23?jobarea=040000'


def yjs_packet(body, url='https://youngapi.yingjiesheng.com/open/yjs/apply', job_id='123'):
    return SimpleNamespace(url=url, request=SimpleNamespace(postData={'jobIDs': job_id}),
                           response=SimpleNamespace(body=body, status=200))


class YjsListener(Listener):
    def start(self, *args, **kw):
        self.events.append(('listen', args, kw))
    def wait(self, **kw):
        self.events.append(('wait', kw))
        return super().wait(**kw)


class YjsTab:
    def __init__(self, events, packets=(), tab_id='detail', *, tip='', label='立即申请', dialog=False):
        self.events, self.tab_id = events, tab_id
        self.url = 'about:blank'
        self.listen = YjsListener(events, packets)
        self.tip, self.label, self.dialog = tip, label, dialog
        self.page_element = None
    def get(self, url):
        self.events.append(('get', url))
        self.url = url
    def close(self): self.events.append(('close', self.tab_id))
    def run_js(self, script, *args):
        if '.search-list-pagination' in script:
            assert args == ('2',)
            assert "textContent.trim()===arguments[0]" in script
            return self.page_element
        if '.delivery-tip' in script:
            assert 'getClientRects' in script and 'visibility' in script
            return self.tip
        if '.delivery-btn' in script:
            assert 'getClientRects' in script and 'visibility' in script
            return SimpleNamespace(text=self.label, attr=lambda name:None, click=Button(self.events).click)
        if '.el-dialog' in script:
            return '点击后出现弹窗，请人工处理' if self.dialog else ''
        assert '.detail-title-left-top .job' in script and '.detail-content-common.jobinfo' in script
        return {'title':'产品经理', 'jd':'产品需求 AI 实习', 'company':''}


def yjs_adapter(tab, *, approved=False, existing=()):
    browser = SimpleNamespace(get_tabs=lambda:list(existing), new_tab=lambda:tab)
    return adapters.PlatformAdapter('yingjiesheng', browser,
                                    SimpleNamespace(job='产品经理', approved=approved), '040000')


def search_body(items):
    return {'resultbody': {'searchData': {'joblist': {'items': items}}}}


@pytest.mark.parametrize('existing', [False, True])
def test_yjs_search_listens_before_navigation_and_parses_cards(existing):
    events=[]
    item=dict(jobid='123', jobname='产品经理', coname='公司A', providesalary='10K',
              jobarea='深圳-南山区', workyear='应届生', degree='本科', jobterm='全职',
              jobtag='校招,AI', isapply='1', isReprintJob='0', isExpire='1')
    tab=YjsTab(events, [yjs_packet(json.dumps(search_body([item])))], tab_id='search')
    tab.url=build_search_url('yingjiesheng','产品经理','040000')
    adapter=yjs_adapter(tab, existing=[tab] if existing else [])
    adapter.search()
    assert events[0][0:2] == ('listen', (adapters.YJS_SEARCH_ENDPOINT,))
    assert events[1][0]=='get' and events[2]==('wait', {'timeout':15, 'raise_err':False})
    assert events[-1]=='stop'
    assert adapter.cards()==[dict(job_id='123', title='产品经理', company='公司A', salary='10K',
                                 tags='深圳-南山区 应届生 本科 全职 校招,AI',
                                 href='https://q.yingjiesheng.com/jobdetail/123.html',
                                 url='https://q.yingjiesheng.com/jobdetail/123.html',
                                 already_applied=True, external=True)]


@pytest.mark.parametrize('body', [None, {}, {'resultbody':None}, search_body(None),
                                search_body({}), search_body([None]), 'broken'])
def test_yjs_search_missing_or_malformed_packet_fails_closed(body):
    events=[]
    tab=YjsTab(events, [] if body is None else [yjs_packet(body)])
    adapter=yjs_adapter(tab)
    adapter.yjs_items=[{'jobid':'old'}]
    with pytest.raises(adapters.AdapterError, match='应届生搜索接口未返回岗位数据'):
        adapter.search()
    assert events[-1]=='stop' and adapter.cards()==[]


def test_yjs_next_page_native_click_and_missing_page():
    events=[]
    tab=YjsTab(events,[yjs_packet(search_body([{'jobid':'456'}]))])
    tab.page_element=FilterElement(events,'page-click')
    adapter=yjs_adapter(tab); adapter.search_tab=tab
    assert adapter.next_page(2)
    assert events[0][0]=='listen' and events[1]=='page-click' and events[-1]=='stop'
    assert adapter.cards()[0]['job_id']=='456'
    events.clear();tab.page_element=None
    assert adapter.next_page(2) is False
    assert len(events)==2 and events[-1]=='stop'


@pytest.mark.parametrize('state,expected', [
    ({'isReprintJob':'1'}, (False,'skip','转载岗位需去外站投递')),
    ({'isapply':'1'}, (False,'skip','平台显示已申请')),
    ({'isapply':'0'}, (True,'apply','')),
    ({'isapply':0, 'isIncomplete':'0'}, (True,'apply','')),
    ({'isapply':'unknown'}, (False,'review','岗位申请状态未知')),
    ({}, (False,'review','岗位申请状态未知')),
    (None, (False,'review','岗位申请状态未知')),
    ({'jobjumptype':'2', 'isxyzapply':'0'}, (True,'apply','')),
    ({'jobjumptype':'2', 'isxyzapply':'1'}, (False,'skip','已申请（志愿）')),
    ({'jobjumptype':'2', 'isxyzapply':'3'}, (False,'skip','已申请（志愿）')),
    ({'jobjumptype':'2', 'isxyzapply':'2'}, (False,'skip','已申请该公司其他职位且已被处理')),
    ({'jobjumptype':'2', 'isxyzapply':'4'}, (False,'review','需替换志愿，请人工处理')),
    ({'jobjumptype':'2', 'isxyzapply':'5'}, (False,'review','需补充企业要求的信息，请人工处理')),
    ({'jobjumptype':'2'}, (False,'review','志愿状态未知')),
    ({'jobjumptype':'2', 'isxyzapply':'9'}, (False,'review','志愿状态未知')),
    ({'isapply':'0', 'isIncomplete':True}, (False,'review','简历不完整，请先完善')),
    ({'jobjumptype':'2', 'isxyzapply':'0', 'isIncomplete':'1'}, (False,'review','简历不完整，请先完善')),
])
def test_yjs_apply_state_all_branches(state, expected):
    assert adapters.yjs_apply_state(state)==expected


@pytest.mark.parametrize('approved', [False,True])
@pytest.mark.parametrize('state_id', ['123','999'])
def test_yjs_detail_state_identity_and_owned_tab_cleanup(monkeypatch, approved, state_id):
    events=[]
    state={'jobid':state_id,'jobname':'产品经理','isapply':'0','jobjumptype':'0'}
    tab=YjsTab(events,[yjs_packet({'resultbody':state})])
    adapter=yjs_adapter(tab,approved=approved); adapter.check=lambda:('','')
    ticks=iter(range(100));monkeypatch.setattr(adapters.time,'monotonic',lambda:next(ticks))
    monkeypatch.setattr(adapters.time,'sleep',lambda s:None)
    card={'job_id':'123','title':'产品经理','url':'https://q.yingjiesheng.com/jobdetail/123.html?jobid=123'}
    detail=adapter.open_approved_detail(card) if approved else adapter.open_detail(card)
    assert events[0][0:2]==('listen',(adapters.YJS_DETAIL_ENDPOINT,))
    assert events[1][0]=='get' and events[2][0]=='wait' and events[3]=='stop'
    assert detail['yjs_state']['jobid']==state_id
    assert same_job(card,detail)==(state_id=='123')
    assert detail['job_id']=='123' and detail['company']==''
    assert adapter.action_tab is tab and adapter.owned_details=={'detail':tab}
    adapter.close_detail()
    assert events[-1]==('close','detail') and adapter.yjs_state is None


@pytest.mark.parametrize('body', [None,{}, {'resultbody':[]}, 'broken'])
def test_yjs_detail_missing_state_stops_listener_and_cleans_owned_tab(body):
    events=[];tab=YjsTab(events,[] if body is None else [yjs_packet(body)])
    adapter=yjs_adapter(tab)
    with pytest.raises(adapters.AdapterError,match='应届生详情接口未返回岗位数据'):
        adapter.open_detail({'job_id':'123','title':'产品经理'})
    assert events[-1]=='stop'
    adapter.close()
    assert events[-1]==('close','detail')


@pytest.mark.parametrize('flag', ['--job-type','--experience','--degree'])
def test_yjs_filters_rejected_before_connection(flag, capsys):
    with pytest.raises(SystemExit) as exc:
        runner.main('yingjiesheng',{'深圳':'040000'},['--job','产品经理',flag,'本科'])
    assert exc.value.code==2 and '关键词 + Jev 硬条件' in capsys.readouterr().err
    adapter=adapters.PlatformAdapter('yingjiesheng',None,SimpleNamespace(**{flag[2:].replace('-','_'):'本科'}),'040000')
    with pytest.raises(adapters.AdapterError,match='关键词.*Jev 硬条件'):adapter.apply_filters()


@pytest.mark.parametrize('platform', ['liepin','yingjiesheng','51job','yupao'])
@pytest.mark.parametrize('flag,reason', [('already_applied','平台显示已申请'),('external','转载或已过期岗位')])
@pytest.mark.parametrize('dry_run', [False,True])
def test_card_platform_state_skips_before_detail_and_jev(tmp_path,monkeypatch,platform,flag,reason,dry_run):
    (tmp_path / 'data' / 'logs').mkdir(parents=True, exist_ok=True)
    cfg=config(tmp_path);cfg['resume_path']='resume.txt';(tmp_path/'resume.txt').write_text('测试简历')
    monkeypatch.setattr(runner.jev,'load_jev_key',lambda:'test-key')
    for name in ('match_title','match_job','latest_match'):
        monkeypatch.setattr(runner.jev,name,lambda *a,**kw:pytest.fail('卡片不可投不得调 Jev/缓存'))
    monkeypatch.setattr(runner,'submit',lambda *a,**kw:pytest.fail('卡片不可投不得提交'))
    adapter=FakeAdapter([dict(job_id='123', title='产品经理', company='公司A', **{flag:True})])
    parsed=runner.parse_args({'深圳':'040000'},['--job','产品经理',*(['--dry-run'] if dry_run else [])],platform=platform)
    stats=runner.execute(platform,parsed,cfg,adapter,tmp_path,rest=lambda *a:None)
    assert stats['skip']==1 and not adapter.opened
    assert load_log(tmp_path / 'data' / 'logs' / f'{platform}-深圳-log.json')['skipped'][0]['reason']==reason


@pytest.mark.parametrize('state', [
    {'isapply':'1'}, {'isReprintJob':'1'}, {'jobjumptype':'2','isxyzapply':'1'},
    {'jobjumptype':'2','isxyzapply':'2'}, {'jobjumptype':'2','isxyzapply':'3'},
    {'jobjumptype':'2','isxyzapply':'4'}, {'jobjumptype':'2','isxyzapply':'5'},
    {'jobjumptype':'2'}, {'isapply':'0','isIncomplete':True}, {},
])
@pytest.mark.parametrize('dry_run', [False,True])
def test_yjs_runner_state_gate_precedes_all_jev(tmp_path,monkeypatch,state,dry_run):
    (tmp_path / 'data' / 'logs').mkdir(parents=True, exist_ok=True)
    cfg=config(tmp_path);cfg['resume_path']='resume.txt';(tmp_path/'resume.txt').write_text('测试简历')
    monkeypatch.setattr(runner.jev,'load_jev_key',lambda:'test-key')
    # 标题初筛在详情前（与其他平台一致）；状态不可投时不得调完整 Jev
    monkeypatch.setattr(runner.jev,'title_protected',lambda title:False)
    monkeypatch.setattr(runner.jev,'match_title',lambda *a,**kw:(False,''))
    monkeypatch.setattr(runner.jev,'latest_match',lambda *a,**kw:None)
    monkeypatch.setattr(runner.jev,'match_job',lambda *a,**kw:pytest.fail('不可投不得调 Jev'))
    monkeypatch.setattr(runner,'submit',lambda *a,**kw:pytest.fail('状态不许可不得提交'))
    adapter=FakeAdapter([dict(job_id='123',title='产品经理',company='公司A')])
    original=adapter.open_detail
    adapter.open_detail=lambda c:{**original(c),'yjs_state':{'jobid':'123',**state}}
    flags=['--dry-run'] if dry_run else ['--calibrate','--job-id','123','--count','1']
    parsed=runner.parse_args({'深圳':'040000'},['--job','产品经理',*flags],platform='yingjiesheng')
    stats=runner.execute('yingjiesheng',parsed,cfg,adapter,tmp_path,rest=lambda *a:None)
    ok,name,reason=adapters.yjs_apply_state(state)
    assert not ok and stats[name]==1 and adapter.opened==['123']
    bucket='review' if name=='review' else 'skipped'
    assert load_log(tmp_path / 'data' / 'logs' / 'yingjiesheng-深圳-log.json')[bucket][0]['reason']==reason


@pytest.mark.parametrize('outer,nested,expected', [
    ('1','1','applied'),(1,1,'applied'),('1','-102','failed'),
    ('1','-100','failed'),('1','-101','failed'),('1','0','failed'),
    ('600302','1','failed'),('600402','1','failed'),('600403','1','failed'),
    ('600404','1','failed'),('600406','1','failed'),('600410','1','failed'),
    (None,'1','failed'),('1',None,'failed'),(True,True,'failed'),
])
def test_yjs_nested_response_classification_and_short_evidence(outer,nested,expected):
    body={'status':outer,'resultbody':{'items':[{'status':nested,'private':'PRIVATE'}]},'msg':'account@example.test'}
    info=rt.packet_info(yjs_packet(body),'123')
    assert info['outer_status']==(None if outer is None or isinstance(outer,bool) else str(outer))
    assert info['nested_status']==(None if nested is None or isinstance(nested,bool) else str(nested))
    assert rt.classify_response(info,adapters.VERIFIED_ENDPOINTS['yingjiesheng'])==expected
    assert 'PRIVATE' not in json.dumps(info) and 'account@' not in json.dumps(info)
    assert rt.classify_response({**info,'correlated':False},adapters.VERIFIED_ENDPOINTS['yingjiesheng'])=='pending'


@pytest.mark.parametrize('items', [None, [], {}, [None], ['bad'], [{'status':{}}]])
def test_yjs_nested_malformed_response_not_success(items):
    info=rt.packet_info(yjs_packet({'status':'1','resultbody':{'items':items}}),'123')
    assert info['nested_status'] is None
    assert rt.classify_response(info,adapters.VERIFIED_ENDPOINTS['yingjiesheng'])=='failed'


@pytest.mark.parametrize('platform', ['yingjiesheng','liepin','51job','yupao'])
def test_apply_now_label_only_allowed_for_yjs(platform):
    adapter=adapters.PlatformAdapter(platform,None,SimpleNamespace(),'040000')
    adapter.yjs_state={'isapply':'0'}
    adapter.detail_tab=YjsTab([])
    adapter._button=lambda c:SimpleNamespace(text=' 立即申请 ',attr=lambda name:None)
    assert (adapter.verified_button({}) is not None)==(platform=='yingjiesheng')


def test_yjs_delivery_tip_prevents_click_and_preserves_reason(checks):
    events=[];tab=YjsTab(events,tip='需补充企业信息')
    adapter=yjs_adapter(tab);adapter.detail_tab=tab;adapter.yjs_state={'jobid':'123','isapply':'0'}
    tab.url='https://q.yingjiesheng.com/jobdetail/123.html'
    card={'job_id':'123','title':'产品经理'}
    assert adapter.button_state(card)=='立即申请' and adapter.verified_button(card) is None
    status,reason,evidence=rt.submit(adapter,card,lambda:pytest.fail('tip 不能预记或点击'))
    assert status=='review' and reason=='需补充企业信息' and not evidence['attempted']
    assert events==[]


def test_yjs_post_click_dialog_stays_pending_without_dialog_click(checks):
    events=[];tab=YjsTab(events,dialog=True)
    adapter=yjs_adapter(tab);adapter.detail_tab=tab;adapter.yjs_state={'jobid':'123','isapply':'0'}
    tab.url='https://q.yingjiesheng.com/jobdetail/123.html'
    card={'job_id':'123','title':'产品经理'}
    status,reason,evidence=rt.submit(adapter,card,lambda:events.append('persist'),timeout=.01)
    assert status=='pending' and '弹窗' in reason and evidence['attempted']
    assert events[1:]==['persist','click','stop']


def test_uncalibrated_live_gate_before_browser(tmp_path, monkeypatch):
    # 全部平台已校准后，用临时移出 CALIBRATED 的方式验证门禁仍生效
    monkeypatch.setattr(adapters, 'CALIBRATED', {'liepin'})
    parsed=runner.parse_args({'深圳':'shenzhen'},['--job','产品经理','--matcher','keyword'],platform='yupao')
    adapter=adapters.PlatformAdapter('yupao',None,parsed,'shenzhen')
    stats=runner.execute('yupao',parsed,config(tmp_path),adapter,tmp_path,rest=lambda *a:None)
    assert '尚未首单校准' in stats['stop_reason'] and stats['views']==0


@pytest.mark.parametrize('code,expected', [(0,'applied'),(1,'failed')])
def test_yupao_im_call_classification(code,expected):
    info={'url':'https://yupao-prod.yupaowang.com/job/v3/contact/job/im/call','http_status':200,
          'correlated':True,'code':code,'blocked':False}
    assert rt.classify_response(info,adapters.VERIFIED_ENDPOINTS['yupao'])==expected


@pytest.mark.parametrize('state', [{'isapply':'0'}, {'jobjumptype':'2','isxyzapply':'0'}])
def test_yjs_title_filter_then_detail_state_then_jev(tmp_path,monkeypatch,state):
    events=[]
    cfg=config(tmp_path);cfg['resume_path']='resume.txt';(tmp_path/'resume.txt').write_text('测试简历')
    monkeypatch.setattr(runner.jev,'load_jev_key',lambda:'test-key')
    monkeypatch.setattr(runner.jev,'latest_match',lambda *a,**kw:None)
    monkeypatch.setattr(runner.jev,'title_protected',lambda title:False)
    def title(*a,**kw):
        events.append('title')
        assert events==['title']
        return False,''
    def job(*a,**kw):
        events.append('job')
        assert events==['title','detail','job']
        return {'decision':{'decision':'review','reason':'待复核'}}
    monkeypatch.setattr(runner.jev,'match_title',title)
    monkeypatch.setattr(runner.jev,'match_job',job)
    adapter=FakeAdapter([dict(job_id='123',title='产品经理',company='公司A')])
    def detail(card):
        events.append('detail')
        return {**card,'jd':'产品 AI','yjs_state':{'jobid':'123',**state}}
    adapter.open_detail=detail
    parsed=runner.parse_args({'深圳':'040000'},['--job','产品经理','--dry-run'],platform='yingjiesheng')
    stats=runner.execute('yingjiesheng',parsed,cfg,adapter,tmp_path,rest=lambda *a:None)
    assert stats['review']==1 and events==['title','detail','job']


@pytest.mark.parametrize('state,expected', [({'isapply':'0'},'applied'),
                                         ({'jobjumptype':'2','isxyzapply':'4'},'review')])
def test_yjs_approved_state_gate_and_one_attempt(tmp_path,monkeypatch,no_jev_approved,state,expected):
    seed_approved(tmp_path,'yingjiesheng')
    adapter=ApprovedAdapter();adapter.calibrated=False
    original=adapter.open_approved_detail
    adapter.open_approved_detail=lambda c:{**original(c),'yjs_state':{'jobid':c['job_id'],**state}}
    submitted=[]
    def applied(a,c,pre,**kw):
        pre();submitted.append(c['job_id'])
        assert load_log(tmp_path / 'data' / 'logs' / 'yingjiesheng-深圳-log.json')['pending'][-1]['job_id']==c['job_id']
        return 'applied','接口确认',{'attempted':True}
    monkeypatch.setattr(runner,'submit',applied)
    parsed=runner.parse_args({'深圳':'040000'},['--approved','--calibrate','--job-id','2','--count','1'],platform='yingjiesheng')
    stats=runner.execute('yingjiesheng',parsed,config(tmp_path),adapter,tmp_path,rest=lambda *a:None)
    assert adapter.opened==['2'] and stats[expected]==1
    if expected=='applied':
        assert submitted==['2'] and '已尝试一次' in stats['stop_reason']
        assert load_log(tmp_path / 'data' / 'logs' / 'yingjiesheng-深圳-log.json')['applied'][0]['reason'].startswith('[人工批准]')
    else:
        assert submitted==[] and stats['pending']==0


def test_yjs_approved_uncalibrated_still_refused(tmp_path,no_jev_approved):
    seed_approved(tmp_path,'yingjiesheng')
    adapter=ApprovedAdapter();adapter.calibrated=False
    parsed=runner.parse_args({'深圳':'040000'},['--approved'],platform='yingjiesheng')
    stats=runner.execute('yingjiesheng',parsed,config(tmp_path),adapter,tmp_path,rest=lambda *a:None)
    assert '尚未首单校准' in stats['stop_reason'] and not adapter.opened


def test_yjs_button_rechecks_url_title_and_api_identity(checks):
    tab=YjsTab([]);adapter=yjs_adapter(tab);adapter.detail_tab=tab
    tab.url='https://q.yingjiesheng.com/jobdetail/123.html'
    adapter.yjs_state={'jobid':'123','isapply':'0'}
    card={'job_id':'123','title':'产品经理'}
    assert adapter.verified_button(card) is not None
    assert adapter.verified_button({**card,'title':'销售'}) is None
    tab.url='https://q.yingjiesheng.com/jobdetail/999.html'
    assert adapter.verified_button(card) is None
    tab.url='https://q.yingjiesheng.com/jobdetail/123.html'
    adapter.yjs_state['jobid']='999'
    assert adapter.verified_button(card) is None


def test_yjs_submit_correlated_network_evidence(checks,monkeypatch):
    events=[]
    packet=yjs_packet({'status':'1','resultbody':{'items':[{'status':'1'}]}})
    tab=YjsTab(events,[packet]);adapter=yjs_adapter(tab);adapter.detail_tab=tab
    tab.url='https://q.yingjiesheng.com/jobdetail/123.html'
    adapter.yjs_state={'jobid':'123','isapply':'0'}
    ticks=iter(range(1000));monkeypatch.setattr(rt.time,'monotonic',lambda:next(ticks)/100)
    status,reason,evidence=rt.submit(adapter,{'job_id':'123','title':'产品经理'},lambda:events.append('persist'),timeout=.1)
    assert status=='applied' and reason=='接口确认' and evidence['packets'][0]['correlated']
    assert events[1:3]==['persist','click'] and events[-1]=='stop'


def test_yjs_dialog_after_confirmed_success_stays_applied(checks,monkeypatch):
    # 2026-10-14 首单实测：接口两层 status=1 后页面弹出对话框，不应改判为待核验
    events=[]
    tab=YjsTab(events,[yjs_packet({'status':'1','resultbody':{'items':[{'status':'1'}]}})])
    adapter=yjs_adapter(tab);adapter.detail_tab=tab;adapter.yjs_state={'jobid':'123','isapply':'0'}
    tab.url='https://q.yingjiesheng.com/jobdetail/123.html'
    base=tab.listen.wait
    def wait(**kw):
        packet=base(**kw)
        tab.dialog=True
        return packet
    tab.listen.wait=wait
    ticks=iter(range(1000));monkeypatch.setattr(rt.time,'monotonic',lambda:next(ticks)/100)
    status,reason,evidence=rt.submit(adapter,{'job_id':'123','title':'产品经理'},lambda:events.append('persist'),timeout=.1)
    assert status=='applied' and reason=='接口确认' and len(evidence['packets'])==1
    assert events.count('click')==1 and events[-1]=='stop'


def test_yjs_runner_keeps_original_view_pacing(tmp_path):
    cards=[dict(job_id=str(i),title='产品经理',company=f'公司{i}') for i in range(1,6)]
    adapter=FakeAdapter(cards);original=adapter.open_detail
    adapter.open_detail=lambda c:{**original(c),'yjs_state':{'jobid':c['job_id'],'isapply':'0'}}
    parsed=runner.parse_args({'深圳':'040000'},['--job','产品经理','--count','5','--dry-run','--matcher','keyword'],platform='yingjiesheng')
    rests=[]
    stats=runner.execute('yingjiesheng',parsed,config(tmp_path),adapter,tmp_path,rest=lambda interval,check:rests.append(interval))
    assert stats['views']==5 and rests==[(15,30)]*5+[(90,180)]
    assert rt.PLATFORM_PACING['yingjiesheng']['max_views']==20


@pytest.mark.parametrize('platform,urls', [
    ('yupao', ('https://yupaowang.com/submit', 'https://yupao-prod.yupaowang.com/submit',
               'https://www.yupao.com/submit', 'https://snssdk.yupaowang.com/analytics')),
    ('liepin', ('https://liepin.com/submit', 'https://api-c.liepin.com/submit')),
])
@pytest.mark.parametrize('verified', [False, True])
def test_evidence_domains_capture_and_classify(checks, monkeypatch, platform, urls, verified):
    events=[]
    adapter=SubmitAdapter(events,ui=False)
    real=adapters.PlatformAdapter(platform,None,SimpleNamespace(),'123')
    adapter.domain,adapter.evidence_domains=real.domain,real.evidence_domains
    adapter.verified_endpoints={urls[0]:0} if verified else {}
    packets=[SimpleNamespace(url=url+'?jobId=1&token=SECRET',
                             request=SimpleNamespace(postData='jobId=1'),
                             response=SimpleNamespace(status=200,body={'code':0})) for url in urls]
    other_urls=['https://third.test/track?token=SECRET',
                f'https://evil{real.domain}/track', f'https://{real.domain}.evil.test/track',
                'https://yupaowang.com.evil.test/track', 'https://evilyupaowang.com/track']
    if platform=='liepin': other_urls.append('https://yupao-prod.yupaowang.com/submit')
    packets.extend(SimpleNamespace(url=url) for url in other_urls)
    adapter.action_tab=SimpleNamespace(listen=Listener(events,packets))
    # 固定时钟，不等待真实时间；留足监听轮次收取所有假包。
    ticks=iter(range(1000))
    monkeypatch.setattr(rt.time,'monotonic',lambda:next(ticks)/100)
    classified=[]
    classify=rt.classify_response
    def capture(info,endpoints):
        classified.append(info['url'])
        return classify(info,endpoints)
    monkeypatch.setattr(rt,'classify_response',capture)
    status,_,evidence=rt.submit(adapter,{'job_id':'1'},lambda:events.append('persist'),timeout=1)
    assert status==('applied' if verified else 'pending')
    assert classified==list(urls)==[info['url'] for info in evidence['packets']]
    assert all(info['correlated'] for info in evidence['packets'])
    assert evidence['other_urls']==[url.split('://',1)[1].split('?',1)[0] for url in other_urls]
    assert 'SECRET' not in json.dumps(evidence)
    assert events==['listen','persist','click','stop']


class FilterElement:
    def __init__(self,events,name,visible=True):
        self.events,self.name=events,name
        self.states=SimpleNamespace(has_rect=visible)
    def click(self,**kw):
        assert kw=={'by_js':False}
        self.events.append(self.name)


class DegreeTab:
    def __init__(self,events,value,options,*,control=True,selected=True):
        self.events,self.value,self.options=events,value,options
        self.control=FilterElement(events,'open') if control else None
        self.selected=selected
    def ele(self,selector,**kw):
        assert selector==('xpath://span[contains(@class,"ant-select-selection-placeholder") and text()="学历"]'
                          '/ancestor::div[contains(@class,"ant-select-selector")][1]')
        return self.control
    def eles(self,selector,**kw):
        assert selector==f'css:div.ant-select-item-option[title="{self.value}"]'
        assert self.events==['open',1.5]
        return self.options
    def run_js(self,script,value):
        assert '.ant-select-selection-item' in script and value==self.value
        assert 'getAttribute("title")' in script and 'textContent.trim()' in script
        self.events.append('verify')
        return self.selected


@pytest.mark.parametrize('degree', ['博士','硕士','本科','大专','中专/中技','高中','初中及以下'])
def test_liepin_degree_native_visible_option(monkeypatch,degree):
    events=[]
    monkeypatch.setattr(adapters.time,'sleep',events.append)
    adapter=adapters.PlatformAdapter('liepin',None,SimpleNamespace(degree=degree),'123')
    adapter.check=lambda:('','')
    options=[FilterElement(events,'hidden',False),FilterElement(events,'selected'),
             FilterElement(events,'duplicate')]
    adapter.search_tab=DegreeTab(events,degree,options)
    adapter.apply_filters()
    assert events==['open',1.5,'selected',3,'verify']


@pytest.mark.parametrize('case', ['missing_control','no_options','hidden_options','bad_selection'])
def test_liepin_degree_fails_closed(monkeypatch,case):
    events=[]
    monkeypatch.setattr(adapters.time,'sleep',events.append)
    adapter=adapters.PlatformAdapter('liepin',None,SimpleNamespace(degree='本科'),'123')
    adapter.check=lambda:('','')
    options=[] if case=='no_options' else [FilterElement(events,'selected',case!='hidden_options')]
    adapter.search_tab=DegreeTab(events,'本科',options,control=case!='missing_control',
                                 selected=case!='bad_selection')
    message='猎聘学历选中校验失败：本科' if case=='bad_selection' else '猎聘未找到学历选项：本科'
    with pytest.raises(adapters.AdapterError,match=message): adapter.apply_filters()
    assert ('selected' in events)==(case=='bad_selection')


@pytest.mark.parametrize('degree', ['本科,硕士','本科，硕士','本科,本科'])
def test_liepin_degree_multiple_rejected(degree):
    adapter=adapters.PlatformAdapter('liepin',None,SimpleNamespace(degree=degree),'123')
    adapter.check=lambda:pytest.fail('多值不得操作页面')
    with pytest.raises(adapters.AdapterError,match='只支持一个'): adapter.apply_filters()


@pytest.mark.parametrize('flag', ['--job-type','--experience','--degree',None])
def test_yupao_filters_rejected_but_defaults_empty(flag):
    flags=[] if flag is None else [flag,'本科' if flag=='--degree' else '实习']
    parsed=runner.parse_args({'深圳':'123'},['--job','AI产品实习',*flags],platform='yupao')
    adapter=adapters.PlatformAdapter('yupao',None,parsed,'123')
    adapter.check=lambda:pytest.fail('鱼泡筛选不得操作页面')
    if flag:
        with pytest.raises(adapters.AdapterError,match='鱼泡无实习/学历/经验筛选.*AI产品实习.*Jev 硬条件'):
            adapter.apply_filters()
    else:
        assert parsed.job_type==parsed.experience==parsed.degree==''
        adapter.apply_filters()


@pytest.mark.parametrize('experience,expected', [
    ('在校生','在校生/应届生'),('应届生','在校生/应届生'),
    ('在校生,应届生','在校生/应届生'),('在校生/应届生','在校生/应届生'),
    ('1-3年','1-3年'),('应届生,在校生','应届生,在校生'),
])
def test_51job_experience_alias_native_selection(monkeypatch,experience,expected):
    events=[]
    monkeypatch.setattr(adapters.time,'sleep',lambda seconds:None)
    adapter=adapters.PlatformAdapter('51job',None,SimpleNamespace(experience=experience),'123')
    adapter.check=lambda:('','')
    control=FilterElement(events,'open')
    option=FilterElement(events,'selected')
    def find_control(script,label):
        assert label=='工作年限'
        return control
    def find_option(script,value):
        assert value==expected
        return option
    control.run_js=find_option
    adapter.search_tab=SimpleNamespace(run_js=find_control)
    adapter.apply_filters()
    assert events==['open','selected']


@pytest.mark.parametrize('platform,default', [('51job',30),('yingjiesheng',20),('liepin',60),('yupao',60)])
def test_platform_max_views_default_and_override(platform,default):
    parsed=runner.parse_args({'深圳':'123'},['--job','产品'],platform=platform)
    assert parsed.max_views==default==rt.PLATFORM_PACING[platform]['max_views']
    for explicit in (1,12,60):
        parsed=runner.parse_args({'深圳':'123'},['--job','产品','--max-views',str(explicit)],platform=platform)
        assert parsed.max_views==explicit


@pytest.mark.parametrize('platform,limit', [('51job',30),('yingjiesheng',20),('liepin',60),('yupao',60)])
def test_main_passes_platform_to_parser(monkeypatch,platform,limit):
    monkeypatch.setattr(runner,'load_config',lambda:{'default_count':2,'min_score':60})
    def execute(p,a,cfg,adapter,root):
        assert p==adapter.platform==platform and a.max_views==limit
        assert adapter.browser is None
        return {'views':0}
    monkeypatch.setattr(runner,'execute',execute)
    assert runner.main(platform,{'深圳':'123'},['--job','产品'])=={'views':0}


@pytest.mark.parametrize('platform,interval,every,extra', [
    ('51job',(6,12),8,(60,120)),('liepin',(6,15),10,(45,90)),
    ('yupao',(6,15),10,(45,90)),
])
def test_runner_uses_platform_view_pacing(tmp_path,platform,interval,every,extra):
    cards=[dict(job_id=str(i),title='产品实习',company=f'公司{i}',salary='',url='',jd='产品 实习 AI')
           for i in range(1,2*every+1)]
    parsed=runner.parse_args({'深圳':'123'},['--job','产品','--count',str(len(cards)),
                                          '--dry-run','--matcher','keyword'],platform=platform)
    rests=[]
    def rest(value,check):
        rests.append(value)
        check()
    stats=runner.execute(platform,parsed,config(tmp_path),FakeAdapter(cards),tmp_path,rest=rest)
    assert stats['views']==len(cards)
    assert rests==([interval]*every+[extra])*2


@pytest.mark.parametrize('explicit,expected', [(None,30),(3,3),(25,25)])
def test_51job_runner_respects_view_limit(tmp_path,explicit,expected):
    cards=[dict(job_id=str(i),title='产品实习',company=f'公司{i}',salary='',url='',jd='产品 实习 AI') for i in range(1,32)]
    flags=[] if explicit is None else ['--max-views',str(explicit)]
    parsed=runner.parse_args({'深圳':'123'},['--job','产品','--count','31',
                                          '--dry-run','--matcher','keyword',*flags],platform='51job')
    adapter=FakeAdapter(cards)
    stats=runner.execute('51job',parsed,config(tmp_path),adapter,tmp_path,rest=lambda *a:None)
    assert stats['views']==len(adapter.opened)==expected
    assert stats['stop_reason']=='已达 --max-views 上限'


class Job51Tab:
    def __init__(self, events, packets=(), cards=()):
        self.events, self.items = events, list(cards)
        self.url = build_search_url('51job', '产品经理', '123')
        self.listen = YjsListener(events, packets)
        self.label, self.disabled = '投递', None
        self.button = SimpleNamespace(text=self.label, attr=lambda name:self.disabled if name=='disabled' else None,
                                      click=lambda **kw:pytest.fail('读取列表 JD 不得点击'))
        self.element = SimpleNamespace(ele=lambda selector, **kw:self.button,
                                       click=lambda **kw:pytest.fail('不得打开详情'))

    def get(self, url):
        self.events.append(('get', url))
        self.url = url

    def run_js(self, script, *args):
        if script == adapters.JOB51_CARDS:
            return [dict(c) for c in self.items]
        if '.el-pager li.number' in script:
            return getattr(self, 'page_element', None)
        assert 'd.jobId' in script and 'd.jobTitle.trim()' in script
        return self.element if any((c['job_id'], c['title'].strip()) == args for c in self.items) else None


def job51_item():
    return dict(jobId='123', jobName='产品经理', jobDescribe='产品需求 AI 实习',
                jobHref='https://jobs.51job.com/shenzhen/123.html?token=SECRET#fragment',
                isApply=False, isFromXyx=False, isReprintJob='0', isJump='0', isExpire=False)


def job51_card():
    return dict(job_id='123', title='产品经理', company='公司A', salary='10K', href='', tags='本科')


def job51_packet(items):
    return SimpleNamespace(response=SimpleNamespace(body={'resultbody': {'job': {'items': items}}}))


def job51_adapter(tab, browser=None):
    browser = browser or SimpleNamespace(new_tab=lambda *a:pytest.fail('不得新开详情标签'))
    adapter = adapters.PlatformAdapter('51job', browser, SimpleNamespace(job='产品经理'), '123')
    adapter.search_tab = tab
    return adapter


@pytest.mark.parametrize('existing', [False, True])
def test_51job_search_last_filter_packet_and_next_page(monkeypatch, existing):
    events=[]
    initial=job51_item(); final={**initial, 'jobDescribe':'筛选后的完整 JD'}
    tab=Job51Tab(events)
    def new_tab():
        events.append('new_tab')
        return tab
    browser=SimpleNamespace(get_tabs=lambda:[tab] if existing else [], new_tab=new_tab)
    adapter=job51_adapter(tab, browser);adapter.search_tab=None
    monkeypatch.setattr(adapters.time,'sleep',lambda seconds:None)
    original_get = tab.get
    # 导航与筛选分别产生一包，最后一包才可用于当前列表。
    def navigate(url):
        original_get(url)
        tab.listen.packets.append(job51_packet([initial]))
    tab.get=navigate
    def filters():
        events.append('filters')
        tab.listen.packets.append(job51_packet([final]))
    adapter.apply_filters=filters
    adapter.search()
    assert adapter.job51_items=={'123':final}
    assert events[0 if existing else 1]==('listen', ('we.51job.com/api/job/search-pc',), {})
    assert events[-1]=='stop'
    assert [e[0] for e in events if isinstance(e,tuple)]==['listen','get','wait','wait','wait']
    # 51job 忽略 URL pageNum：翻页必须原生点击页码，不重新导航
    final={**final, 'jobId':'456'}
    url_before=tab.url
    def click(by_js=None):
        assert by_js is False
        events.append('page-click'); tab.listen.packets.append(job51_packet([final]))
    tab.page_element=SimpleNamespace(click=click)
    assert adapter.next_page(2) and adapter.job51_items=={'456':final}
    assert tab.url==url_before and 'page-click' in events and events.count('stop')==2
    tab.page_element=None
    assert adapter.next_page(9) is False


@pytest.mark.parametrize('body', [None, {}, {'resultbody':None},
    {'resultbody':{'job':{'items':{}}}}, {'resultbody':{'job':{'items':[None]}}},
    {'resultbody':{'job':{'items':[{'jobId':123}]}}}, '不是 JSON'])
def test_51job_search_missing_or_bad_last_packet_clears_stale(monkeypatch, body):
    events=[]
    packets=[] if body is None else [job51_packet([job51_item()]), SimpleNamespace(response=SimpleNamespace(body=body))]
    tab=Job51Tab(events, packets);adapter=job51_adapter(tab)
    adapter.job51_items={'old':job51_item()}
    adapter.apply_filters=lambda:None
    monkeypatch.setattr(adapters.time,'sleep',lambda seconds:None)
    with pytest.raises(adapters.AdapterError,match='51job 搜索接口未返回岗位数据'):
        adapter.search()
    assert adapter.job51_items=={} and events[-1]=='stop'


def test_51job_search_wait_capped_at_20_seconds(monkeypatch):
    events=[];clock=[0];tab=Job51Tab(events);adapter=job51_adapter(tab)
    monkeypatch.setattr(adapters.time,'sleep',lambda seconds:None)
    monkeypatch.setattr(adapters.time,'monotonic',lambda:clock[0])
    adapter.apply_filters=lambda:None
    def wait(timeout, raise_err):
        assert not raise_err and 0 < timeout <= 3
        clock[0]+=min(1, timeout)
        return job51_packet([{**job51_item(),'jobDescribe':str(clock[0])}])
    tab.listen.wait=wait
    adapter.search()
    assert clock[0]==20 and adapter.job51_items['123']['jobDescribe']=='20'
    assert events[-1]=='stop'


@pytest.mark.parametrize('state,applied,external,reason', [
    ({}, False, False, '转载/外跳/过期岗位'),
    ({'isApply':True}, True, False, '转载/外跳/过期岗位'),
    ({'isApply':1}, False, False, '转载/外跳/过期岗位'),
    ({'isFromXyx':True}, False, True, '校招岗位（应届生网），请用 yingjiesheng_apply.py 投递'),
    ({'isReprintJob':'1'}, False, True, '转载/外跳/过期岗位'),
    ({'isJump':'1'}, False, True, '转载/外跳/过期岗位'),
    ({'isExpire':True}, False, True, '转载/外跳/过期岗位'),
])
def test_51job_cards_join_api_by_id_and_title(state,applied,external,reason):
    tab=Job51Tab([],cards=[job51_card()]);adapter=job51_adapter(tab)
    adapter.job51_items={'123':{**job51_item(), 'jobName':' 产品经理 ', **state}}
    card=adapter.cards()[0]
    assert card['jd']=='产品需求 AI 实习' and card['already_applied'] is applied
    assert card['external'] is external and card['external_reason']==reason
    assert card['url']=='https://jobs.51job.com/shenzhen/123.html'


@pytest.mark.parametrize('items', [{}, {'456':job51_item()}, {'123':{**job51_item(),'jobName':'销售'}},
    {'123':{**job51_item(),'jobDescribe':''}}, {'123':{**job51_item(),'jobDescribe':None}}])
def test_51job_unmatched_cards_review_without_jev(tmp_path,monkeypatch,items):
    (tmp_path / 'data' / 'logs').mkdir(parents=True, exist_ok=True)
    tab=Job51Tab([],cards=[job51_card()]);adapter=job51_adapter(tab);adapter.job51_items=items
    adapter.search=lambda:None;adapter.check=lambda:('','');adapter.next_page=lambda n:False
    assert adapter.cards()[0]['jd']==''
    cfg=config(tmp_path);cfg['resume_path']='resume.txt';(tmp_path/'resume.txt').write_text('测试简历')
    monkeypatch.setattr(runner.jev,'load_jev_key',lambda:'test-key')
    for name in ('match_title','match_job','latest_match'):
        monkeypatch.setattr(runner.jev,name,lambda *a,**kw:pytest.fail('空 JD 不得调 Jev/缓存'))
    monkeypatch.setattr(runner,'submit',lambda *a,**kw:pytest.fail('空 JD 不得提交'))
    parsed=runner.parse_args({'深圳':'123'},['--job','产品经理','--dry-run'],platform='51job')
    stats=runner.execute('51job',parsed,cfg,adapter,tmp_path,rest=lambda *a:None)
    assert stats['review']==1 and not stats['applied']
    assert load_log(tmp_path / 'data' / 'logs' / '51job-深圳-log.json')['review'][0]['reason']=='卡片与详情 ID/标题不匹配，或 JD 为空'


@pytest.mark.parametrize('label,disabled,ready', [('投递',None,True),('已投递',None,False),('投递','',False)])
def test_51job_open_detail_uses_list_button_without_click(label,disabled,ready):
    tab=Job51Tab([],cards=[job51_card()]);adapter=job51_adapter(tab)
    tab.button.text=label;tab.disabled=disabled
    card={**job51_card(),'jd':'完整 JD','url':'https://jobs.51job.com/shenzhen/123.html'}
    detail=adapter.open_detail(card)
    assert detail=={k:card[k] for k in ('job_id','title','jd','company','url')} | {'button_state':label,'button_ready':ready}
    assert adapter.current is card and adapter.detail_tab is None
    assert same_job(card,adapter.detail_snapshot())
    assert adapter.verified_button({**card,'job_id':'456'}) is None
    assert adapter.verified_button({**card,'title':'销售'}) is None
    for changed in ({**job51_card(),'job_id':'456'}, {**job51_card(),'title':'销售'}):
        tab.items=[changed]
        assert adapter.verified_button(card) is None
    tab.items=[]
    assert adapter.verified_button(card) is None
    assert adapter.open_detail(card)=={}
    adapter.close_detail()


@pytest.mark.parametrize('dry_run', [False,True])
def test_51job_external_reason_skips_before_jev(tmp_path,monkeypatch,dry_run):
    (tmp_path / 'data' / 'logs').mkdir(parents=True, exist_ok=True)
    cfg=config(tmp_path);cfg['resume_path']='resume.txt';(tmp_path/'resume.txt').write_text('测试简历')
    monkeypatch.setattr(runner.jev,'load_jev_key',lambda:'test-key')
    for name in ('match_title','match_job','latest_match'):
        monkeypatch.setattr(runner.jev,name,lambda *a,**kw:pytest.fail('外跳岗位不得调 Jev/缓存'))
    reason='校招岗位（应届生网），请用 yingjiesheng_apply.py 投递'
    adapter=FakeAdapter([{**job51_card(),'external':True,'external_reason':reason}])
    parsed=runner.parse_args({'深圳':'123'},['--job','产品经理',*(['--dry-run'] if dry_run else [])],platform='51job')
    stats=runner.execute('51job',parsed,cfg,adapter,tmp_path,rest=lambda *a:None)
    assert stats['skip']==1 and not adapter.opened
    assert load_log(tmp_path / 'data' / 'logs' / '51job-深圳-log.json')['skipped'][0]['reason']==reason


def seed_approved(tmp_path, platform='liepin', approved=('1', '2'), ids=('1', '2', '3')):
    (tmp_path / 'data' / 'logs').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'reports').mkdir(parents=True, exist_ok=True)
    from review_list import REVIEW_FILE, render_review_list
    rows = [dict(platform=platform, job_id=i, job='产品实习', company=f'公司{i}',
                 url=(f'https://www.liepin.com/job/{i}.shtml' if platform == 'liepin'
                      else f'https://q.yingjiesheng.com/jobdetail/{i}.html' if platform == 'yingjiesheng'
                      else f'https://www.yupao.com/zhaogong/{i}.html')) for i in ids]
    (tmp_path / 'data' / 'logs' / f'{platform}-深圳-log.json').write_text(json.dumps({'review': rows}))
    (tmp_path / 'reports' / REVIEW_FILE).write_text(render_review_list(rows, {f'{platform}:{i}' for i in approved}))


class ApprovedAdapter(FakeAdapter):
    def search(self): pytest.fail('批准模式不得搜索')
    def cards(self): pytest.fail('批准模式不得读取搜索列表')
    def next_page(self, n): pytest.fail('批准模式不得翻页')
    def open_detail(self, card): pytest.fail('批准模式不得点击搜索卡片')
    def open_approved_detail(self, card): return super().open_detail(card)


@pytest.fixture
def no_jev_approved(monkeypatch):
    def fail(*a, **kw): pytest.fail('批准模式不得读取 key、缓存或调用 Jev/评分')
    for name in ('load_jev_key', 'match_title', 'match_job', 'latest_match'):
        monkeypatch.setattr(runner.jev, name, fail)
    monkeypatch.setattr(runner, 'score_jd', fail)
    monkeypatch.setattr(runner, 'cache_scope', fail)


def test_liepin_approved_only_checked_count_pending_order_and_dedup(tmp_path, monkeypatch, no_jev_approved):
    seed_approved(tmp_path)
    submitted = []

    def applied(adapter, card, pre, **kw):
        pre()
        pending = load_log(tmp_path / 'data' / 'logs' / 'liepin-深圳-log.json')['pending']
        assert pending[-1]['job_id'] == card['job_id']
        submitted.append(card['job_id'])
        return 'applied', '当前岗位按钮确认成功', {'attempted': True}

    monkeypatch.setattr(runner, 'submit', applied)
    a = args('--approved', '--matcher', 'jev', '--count', '1')
    adapter = ApprovedAdapter()
    stats = run(tmp_path, adapter, a)
    assert stats['applied'] == 1 and adapter.opened == ['1']
    run(tmp_path, ApprovedAdapter(), args('--approved', '--matcher', 'jev'))
    assert submitted == ['1', '2']
    log = load_log(tmp_path / 'data' / 'logs' / 'liepin-深圳-log.json')
    assert not log['pending'] and len(log['applied']) == 2
    assert all(r['reason'].startswith('[人工批准]') for r in log['applied'])
    adapter = ApprovedAdapter()
    run(tmp_path, adapter, a)
    assert not adapter.opened


def test_approved_platform_identity_mismatch_no_submit(tmp_path, monkeypatch, no_jev_approved):
    seed_approved(tmp_path)
    monkeypatch.setattr(runner, 'submit', lambda *a, **kw: pytest.fail('身份不符不得提交'))
    stats = run(tmp_path, ApprovedAdapter(bad_detail=True), args('--approved'))
    assert stats['review'] == 2 and not stats['applied']
    assert not load_log(tmp_path / 'data' / 'logs' / 'liepin-深圳-log.json')['pending']


def test_approved_platform_pending_stops_and_preserves_dedup(tmp_path, monkeypatch, no_jev_approved):
    seed_approved(tmp_path)

    def pending(adapter, card, pre, **kw):
        pre()
        return 'pending', '未知结果', {'attempted': True}

    monkeypatch.setattr(runner, 'submit', pending)
    adapter = ApprovedAdapter()
    stats = run(tmp_path, adapter, args('--approved'))
    assert stats['pending'] == 1 and adapter.opened == ['1']
    assert 'liepin:1' in load_applied_index(tmp_path)


def test_approved_platform_cooldown_no_connection(tmp_path, no_jev_approved):
    (tmp_path / 'data' / 'cooldown').mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timedelta
    seed_approved(tmp_path)
    (tmp_path / 'data' / 'cooldown' / 'liepin-cooldown.json').write_text(json.dumps({
        'until': (datetime.now().astimezone() + timedelta(hours=1)).isoformat()}))
    adapter = ApprovedAdapter()
    stats = run(tmp_path, adapter, args('--approved'))
    assert not adapter.opened and '未连接浏览器' in stats['stop_reason']


def test_approved_51job_parse_error(capsys):
    with pytest.raises(SystemExit) as exc:
        runner.parse_args({'深圳': '123'}, ['--approved'], platform='51job')
    assert exc.value.code == 2 and '51job 暂不支持批准投递' in capsys.readouterr().err


def test_approved_uncalibrated_requires_same_calibration_gate(tmp_path, no_jev_approved):
    seed_approved(tmp_path, 'yupao')
    a = runner.parse_args({'深圳': '123'}, ['--approved'], platform='yupao')
    adapter = ApprovedAdapter()
    adapter.calibrated = False
    stats = runner.execute('yupao', a, config(tmp_path), adapter, tmp_path, rest=lambda *a: None)
    assert not adapter.opened and '尚未首单校准' in stats['stop_reason']
    for flags in (['--calibrate'], ['--calibrate', '--job-id', '1'],
                  ['--calibrate', '--job-id', '1', '--count', '1', '--fill-queue']):
        with pytest.raises(SystemExit):
            runner.parse_args({'深圳': '123'}, ['--approved', *flags], platform='yupao')


def test_approved_yupao_calibration_attempts_one_checked_job(tmp_path, monkeypatch, no_jev_approved):
    seed_approved(tmp_path, 'yupao')
    a = runner.parse_args({'深圳': '123'}, ['--approved', '--calibrate', '--count', '1', '--job-id', '2'], platform='yupao')
    adapter = ApprovedAdapter()
    adapter.calibrated = False

    def pending(adapter, card, pre, **kw):
        pre()
        return 'pending', '待核验', {'attempted': True}

    monkeypatch.setattr(runner, 'submit', pending)
    stats = runner.execute('yupao', a, config(tmp_path), adapter, tmp_path, rest=lambda *a: None)
    assert stats['pending'] == 1 and adapter.opened == ['2']


def test_approved_dry_run_no_submit(tmp_path, monkeypatch, no_jev_approved):
    seed_approved(tmp_path)
    monkeypatch.setattr(runner, 'submit', lambda *a, **kw: pytest.fail('dry-run 不得提交'))
    stats = run(tmp_path, ApprovedAdapter(), args('--approved', '--dry-run', '--count', '1'))
    assert stats['evaluated'] == 1 and not stats['applied'] and not stats['pending']


@pytest.mark.parametrize('platform', ['liepin', 'yupao'])
def test_approved_adapter_direct_navigation_identity_and_cleanup(monkeypatch, platform):
    events = []
    monkeypatch.setattr(adapters, 'page_block_reason', lambda tab: '')
    monkeypatch.setattr(adapters, 'page_gate_reason', lambda tab: '')
    a = runner.parse_args({'深圳': '123'}, ['--approved'], platform=platform)
    url = 'https://www.liepin.com/job/123.shtml' if platform == 'liepin' else 'https://www.yupao.com/zhaogong/123.html'
    card = {'job_id': '123', 'title': '产品实习', 'url': url}
    button = SimpleNamespace(text='聊一聊' if platform == 'liepin' else '发送简历', attr=lambda name: None)

    class Tab:
        tab_id = 'owned'
        url = ''
        title = '产品实习'
        def get(self, url):
            events.append(url)
            self.url = url
        def run_js(self, script, *args):
            if 'const roots=' in script:
                return button
            return {'title': self.title, 'jd': '产品实习 JD', 'company': '公司', 'href': url}
        def close(self): events.append('closed')

    tab = Tab()
    adapter = adapters.PlatformAdapter(platform, SimpleNamespace(new_tab=lambda: tab), a, '123')
    snapshot = adapter.open_approved_detail(card)
    assert same_job(card, snapshot) and snapshot['button_ready']
    assert adapter.search_tab is None and adapter.action_tab is tab and events == [url]
    assert adapter.verified_button(card) is button
    tab.title = '销售'
    assert adapter.verified_button(card) is None
    tab.title = '产品实习'
    tab.url = url.replace('123', '999')
    assert adapter.verified_button(card) is None
    adapter.close()
    assert events == [url, 'closed'] and not adapter.owned_details


@pytest.mark.parametrize('outer,msg,expected', [('1','成功','applied'),('1','失败','failed'),('0','成功','failed')])
def test_51job_light_apply_endpoint_classification(outer,msg,expected):
    url='https://cupid.51job.com/open/user-apply/open/user-apply/light-apply-job'
    info={'url':url,'http_status':200,'correlated':True,'outer_status':outer,
          'success_message':msg=='成功','blocked':False}
    assert rt.classify_response(info,adapters.VERIFIED_ENDPOINTS['51job'])==expected


def test_detail_closed_before_post_submit_rest(tmp_path, monkeypatch):
    # 2026-10-15 猎聘批量：成功后详情页聊天窗被休息期 check 误判为简历弹窗而停止
    adapter = FakeAdapter([dict(job_id=str(i), title='产品经理', company=f'公司{i}') for i in (1, 2)])
    adapter.calibrated = True
    events = []
    original_close = adapter.close_detail
    adapter.close_detail = lambda: (events.append('close'), original_close())[1]
    def submit(a, c, pre, **kw):
        pre(); events.append('submit')
        return 'applied', '接口确认', {'attempted': True}
    monkeypatch.setattr(runner, 'submit', submit)
    parsed = runner.parse_args({'深圳': '1'}, ['--job', '产品经理', '--count', '2', '--matcher', 'keyword', '--min-score', '0'])
    stats = runner.execute('liepin', parsed, config(tmp_path), adapter, tmp_path,
                           rest=lambda *a: events.append('rest'))
    assert stats['applied'] == 2
    first = events.index('submit')
    assert events[first + 1] == 'close' and 'rest' in events[first + 2:]
