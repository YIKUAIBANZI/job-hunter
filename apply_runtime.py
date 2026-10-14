"""多平台共用节奏、风控和提交核验。任何未知提交保持 pending。"""
import json
import random
import re
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

CHAT_INTERVAL = (30, 90)
SUCCESS_REST_EVERY = 5
SUCCESS_REST = (180, 300)
VIEW_INTERVAL = (6, 15)
VIEW_REST_EVERY = 10
VIEW_REST = (45, 90)
_DEFAULT_PACING = {'view_interval': VIEW_INTERVAL, 'view_rest_every': VIEW_REST_EVERY,
                   'view_rest': VIEW_REST, 'max_views': 60}
PLATFORM_PACING = {
    'liepin': dict(_DEFAULT_PACING),
    'yupao': dict(_DEFAULT_PACING),
    # 51job 详情风控更敏感，默认降低浏览上限并延长休息。
    '51job': {**_DEFAULT_PACING, 'view_interval': (15, 30), 'view_rest_every': 5,
              'view_rest': (90, 180), 'max_views': 20},
    'yingjiesheng': {**_DEFAULT_PACING, 'view_interval': (15, 30), 'view_rest_every': 5,
                    'view_rest': (90, 180), 'max_views': 20},
}
BLOCK_WORDS = ('上限', '频繁', '验证', '异常', '验证码', '操作过快', '访问受限')


def safe_url(url):
    """证据只保存 host/path，避免签名、token、callback 泄漏。"""
    p = urlsplit(url or '')
    return p._replace(query='', fragment='', netloc=p.hostname or '').geturl()


def write_cooldown(path, reason):
    now = datetime.now().astimezone()
    path.write_text(json.dumps({'until': (now + timedelta(hours=2)).isoformat(),
                               'reason': reason}, ensure_ascii=False), encoding='utf-8')


def pick_tab(tabs, host, path):
    for tab in tabs:
        p = urlsplit(tab.url)
        if p.hostname == host and p.path.startswith(path):
            return tab
    return None


def page_block_reason(page):
    p = urlsplit(page.url)
    if p.hostname == 'safe.liepin.com' or re.search(r'verify|security|captcha|intercept|verifysms', p.path, re.I):
        return '页面跳转至验证/安全检查'
    return page.run_js('''
        const visible=e=>e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
        for (const e of document.querySelectorAll('.geetest_panel,.nc-container,[id*="captcha"],[class*="captcha"],iframe[src*="verify"],iframe[src*="captcha"]')) {
            if(visible(e)) return '页面出现验证组件';
        }
        for (const e of document.querySelectorAll('[role="dialog"],.ant-modal,.el-message,.ant-message,.dialog,.modal')) {
            // 带上弹层原文与 class，便于区分真实验证码和普通弹窗误判
            if(visible(e)&&/上限|频繁|验证|异常|操作过快|访问受限/.test(e.innerText||''))
                return '页面弹层提示风控：' + String(e.className).slice(0,40) + '｜' + (e.innerText||'').replace(/\\s+/g,' ').slice(0,120);
        }
        return '';
    ''') or ''


def page_gate_reason(page):
    path = urlsplit(page.url).path.lower()
    if 'resume/guide' in path:
        return '请先完善在线简历'
    if 'login' in path or 'passport' in path:
        return '请先登录平台'
    return page.run_js('''
        for(const e of document.querySelectorAll('[role="dialog"],.ant-modal,.login-dialog,.login-modal')) {
            if(e.getClientRects().length && /登录|完善简历/.test(e.innerText||'')) return '登录或简历弹窗待处理';
        }
        return '';
    ''') or ''


def request_job_ids(value):
    """只读明确的职位字段，绝不以任意字符串包含 ID 作为关联依据。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = parse_qs(value)
    found = set()
    if isinstance(value, dict):
        for key, val in value.items():
            if key.lower().replace('_', '') in ('jobid', 'jobids', 'positionid', 'recruitid'):
                vals = val if isinstance(val, list) else [val]
                for item in vals:
                    if isinstance(item, (str, int)) and not isinstance(item, bool):
                        found.update(str(item).split(','))
            elif isinstance(val, (dict, list)):
                found.update(request_job_ids(val))
    elif isinstance(value, list):
        for val in value:
            found.update(request_job_ids(val))
    return found


def packet_info(packet, job_id, aliases=()):
    body = packet.response.body
    body_type = type(body).__name__
    if isinstance(body, (str, bytes)):
        try:
            body = json.loads(body)
        except (ValueError, UnicodeError):
            body = {}
    body = body if isinstance(body, dict) else {}
    ids = request_job_ids(packet.request.postData) | request_job_ids(parse_qs(urlsplit(packet.url).query))
    message = str(body.get('message') or body.get('msg') or '')
    # 只取前端成功判定需要的短码，不保存 resultbody 原文。
    items = (body.get('resultbody') or {}).get('items') if isinstance(body.get('resultbody'), dict) else None
    nested = items[0].get('status') if isinstance(items, list) and items and isinstance(items[0], dict) else None
    def status_code(value):
        return str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None
    # 不保存请求体或原始消息（其中可能含用户信息），保留判定需要的分类。
    return {'url': safe_url(packet.url), 'http_status': packet.response.status,
            'code': body.get('code') if isinstance(body.get('code'), (str, int)) and not isinstance(body.get('code'), bool) else None,
            'body_type': body_type,
            'outer_status': status_code(body.get('status')),
            'nested_status': status_code(nested),
            'body_fields': sorted(str(k) for k in body)[:20],
            # 仅保存非身份类业务标志用于首单校准；不据此推断未知协议成功。
            'business_flags': {k: body[k] for k in ('flag', 'success', 'status', 'statusCode')
                               if isinstance(body.get(k), (bool, int, float))},
            'request_job_ids': sorted(ids),
            'correlated': bool(ids & ({str(job_id)} | {str(a) for a in aliases})),
            'success_message': message.strip().lower() in ('success', 'ok', '成功', '投递成功', '申请成功', '发送成功'),
            'blocked': any(w in message for w in BLOCK_WORDS)}


def classify_response(info, verified_endpoints=()):
    if info.get('blocked') or info.get('http_status') == 429:
        return 'blocked'
    if info.get('url') not in verified_endpoints or not info.get('correlated'):
        return 'pending'
    # 各平台成功码必须由真实首单校准后提供，禁止套用 Boss code=0。
    # 成功条件为 code 值、(业务字段, 值)，或只读脱敏证据的判定函数。
    for endpoint, expect in verified_endpoints.items():
        if info['url'] == endpoint:
            if callable(expect):
                ok = expect(info)
            elif isinstance(expect, tuple):
                ok = (info.get('business_flags') or {}).get(expect[0]) == expect[1]
            else:
                ok = info.get('code') == expect
            return 'applied' if info['http_status'] == 200 and ok else 'failed'
    return 'pending'


def submit(adapter, card, before_click, *, dry_run=False, timeout=8):
    """before_click 必须持久化 pending。先监听、再核身份、再原生点击一次。"""
    evidence = {'packets': [], 'ui_before': '', 'ui_after': '', 'attempted': False}
    if dry_run:
        return 'skipped', 'dry-run 禁止提交', evidence
    tab = adapter.action_tab
    listening = False
    try:
        reason = page_block_reason(tab)
        if reason:
            return 'blocked', reason, evidence
        reason = page_gate_reason(tab)
        if reason:
            return 'review', reason, evidence
        evidence['ui_before'] = adapter.button_state(card)
        if evidence['ui_before'] in adapter.success_texts:
            return 'skipped', '已投递或已沟通', evidence
        button = adapter.verified_button(card)
        if button is None:
            return 'review', getattr(adapter, 'button_reason', '') or '按钮身份不匹配或不可用', evidence
        # 捕获正常 UI 产生的 XHR/Fetch；不重放接口、不记录鉴权数据。
        tab.listen.start(True, method=True, res_type=('XHR', 'Fetch'))
        listening = True
        button = adapter.verified_button(card)
        if button is None:
            return 'review', getattr(adapter, 'button_reason', '') or '点击前岗位发生变化', evidence
        before_click()
        evidence['attempted'] = True
        button.click(by_js=False)
        deadline = time.monotonic() + timeout
        network = 'pending'
        while time.monotonic() < deadline:
            reason = page_block_reason(tab)
            if reason:
                return 'blocked', reason, evidence
            if hasattr(adapter, 'submission_gate_reason'):
                reason = adapter.submission_gate_reason()
                if reason:
                    return 'pending', reason, evidence
            packet = tab.listen.wait(timeout=min(.5, max(.01, deadline-time.monotonic())), raise_err=False)
            if packet:
                # 页面与业务接口可能使用不同域名；同平台埋点仍由精确端点核验排除。
                host = urlsplit(packet.url).hostname or ''
                domains = getattr(adapter, 'evidence_domains', (adapter.domain,))
                if any(host == domain or host.endswith('.' + domain) for domain in domains):
                    info = packet_info(packet, card['job_id'], adapter.request_id_aliases(card['job_id'])
                                       if hasattr(adapter, 'request_id_aliases') else ())
                    evidence['packets'].append(info)
                    result = classify_response(info, adapter.verified_endpoints)
                    if result == 'blocked':
                        return 'blocked', '接口风控或限流', evidence
                    if result in ('applied', 'failed'):
                        network = result
                else:
                    # 首单诊断：其他域名只记 host+path，用于找到真实提交端点
                    other = evidence.setdefault('other_urls', [])
                    url = f"{host}{urlsplit(packet.url).path}"
                    if url not in other and len(other) < 30:
                        other.append(url)
            # 只接受同一岗位的按钮发生成功状态变化；全页出现“成功”不算。
            evidence['ui_after'] = adapter.button_state(card)
        if hasattr(adapter, 'submission_gate_reason'):
            reason = adapter.submission_gate_reason()
            if reason:
                return 'pending', reason, evidence
        if network == 'failed':
            return 'pending', '接口与页面结果需人工核验', evidence
        if network == 'applied' or (evidence['ui_after'] in adapter.success_texts
                                    and evidence['ui_before'] not in adapter.success_texts):
            return 'applied', '接口确认' if network == 'applied' else '当前岗位按钮确认成功', evidence
        return 'pending', page_gate_reason(tab) or '点击后未获得明确成功证据', evidence
    except Exception as exc:
        try:
            reason = page_block_reason(tab)
            if reason:
                return 'blocked', reason, evidence
        except Exception:
            pass
        return ('pending' if evidence['attempted'] else 'review'), f'核验中断：{type(exc).__name__}', evidence
    finally:
        if listening:
            try:
                tab.listen.stop()
            except Exception:
                pass


def paced_rest(interval, check, sleep=time.sleep):
    """每秒检查一次风控，长休息期间也能及时停止。"""
    remaining = random.uniform(*interval)
    while remaining > 0:
        check()
        step = min(1, remaining)
        sleep(step)
        remaining -= step
    check()
