import json
from types import SimpleNamespace

import pytest

import apply_runtime as rt
import platform_runner as runner
import platform_adapters as adapters
from platform_adapters import build_search_url, parse_job_id, same_job
from shared import load_applied_index, load_log, load_config


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
    for platform in ('51job','liepin','yupao','boss'):
        (tmp_path/f'{platform}-old-log.json').write_text(json.dumps({'pending':[{'jobId':'123','company':'A','job':'B'}]}))
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
    log=load_log(tmp_path/'liepin-深圳-log.json')
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
    assert not load_log(tmp_path/'liepin-深圳-log.json')['pending']


def test_block_writes_cooldown_before_next_run(tmp_path):
    stats=run(tmp_path,FakeAdapter(block=True))
    assert '验证' in stats['stop_reason']
    assert (tmp_path/'liepin-cooldown.json').exists()
    adapter=FakeAdapter();run(tmp_path,adapter)
    assert adapter.searches==0
    assert load_log(tmp_path/'liepin-深圳-log.json')['failed'][0]['status']=='blocked'


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
    a=args('--dry-run','--count','5','--matcher','jev')
    stats=runner.execute('liepin',a,cfg,FakeAdapter(),tmp_path,rest=lambda *a:None)
    assert stats['review']==3 and '3' in stats['stop_reason']


def test_cache_context_changes():
    cfg={'exclude_keywords':[]}
    assert runner.cache_scope('51job',cfg,'a','x') != runner.cache_scope('51job',cfg,'b','x')
    assert runner.cache_scope('51job',cfg,'a','x') != runner.cache_scope('liepin',cfg,'a','x')


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
    log=load_log(tmp_path/'liepin-深圳-log.json')
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
    info=rt.packet_info(packet,'1985442155')
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
    pkt=SimpleNamespace(url=url,request=SimpleNamespace(postData='{"jobId":"75191697"}'),
                        response=SimpleNamespace(status=200,body={'flag':1,'data':{}}))
    adapter=adapters.PlatformAdapter.__new__(adapters.PlatformAdapter); adapter.platform='liepin'
    info=rt.packet_info(pkt,'1975191697',adapter.request_id_aliases('1975191697'))
    assert info['correlated']
    assert rt.classify_response(info,adapters.VERIFIED_ENDPOINTS['liepin'])=='applied'
    bad={**info,'business_flags':{'flag':0}}
    assert rt.classify_response(bad,adapters.VERIFIED_ENDPOINTS['liepin'])=='failed'
    assert rt.packet_info(pkt,'1975191697')['correlated'] is False  # 无别名不关联


def test_only_liepin_calibrated_and_alias_scoped():
    a=adapters.PlatformAdapter.__new__(adapters.PlatformAdapter)
    a.platform='yupao'
    assert a.request_id_aliases('1975191697')==set()
    assert adapters.CALIBRATED=={'liepin'}
