"""平台 DOM 适配器。选择器依据 evidence 中的公开页面检查，未知结构失败关闭。"""
import json
import re
import time
from urllib.parse import urlencode, urlsplit

from apply_runtime import page_block_reason, page_gate_reason, pick_tab, safe_url

class AdapterError(Exception):
    """可安全展示的适配器错误，不含原始网络请求。"""


DOMAINS = {'liepin': 'liepin.com', '51job': '51job.com', 'yupao': 'yupao.com'}
EVIDENCE_DOMAINS = {
    'liepin': ('liepin.com',),
    '51job': ('51job.com',),
    'yupao': ('yupao.com', 'yupaowang.com'),
}
PATTERNS = {
    'liepin': r'^/(?:job|a)/(\d+)\.shtml$',
    '51job': r'^/[^/]+/(\d+)\.html$',
    'yupao': r'^/zhaogong/(\d+)(?:/[A-Za-z0-9_-]+)?\.html$',
}


def parse_job_id(platform, href):
    try:
        p = urlsplit(href or '')
        domain = DOMAINS[platform]
        if p.scheme not in ('', 'http', 'https'):
            return ''
        if p.netloc and not (p.hostname == domain or (p.hostname or '').endswith('.' + domain)):
            return ''
        match = re.fullmatch(PATTERNS[platform], p.path)
        return match[1] if match else ''
    except (ValueError, KeyError):
        return ''


def same_job(card, detail):
    return bool(card.get('job_id') and detail.get('job_id') == card['job_id']
                and card.get('title') and detail.get('title', '').strip() == card['title'].strip())


def build_search_url(platform, job, city_code, page=1):
    if page < 1:
        raise ValueError('页码必须为正数')
    if platform == 'liepin':
        return 'https://www.liepin.com/zhaopin/?' + urlencode({'key': job, 'dqs': city_code, 'currentPage': page-1})
    if platform == '51job':
        return 'https://we.51job.com/pc/search?' + urlencode({'keyword': job, 'jobArea': city_code, 'pageNum': page})
    if platform == 'yupao':
        return f'https://www.yupao.com/topic/{city_code}/?' + urlencode({'keywords': job})
    raise ValueError('未知平台')


LIEPIN_CARDS = '''
return Array.from(document.querySelectorAll('a[data-nick="job-detail-job-info"]')).map(a=>{
 const c=a.closest('.jobDetailBox');
 return {href:a.href,title:a.querySelector('.jobTitleBox [title]')?.textContent.trim()||'',
 company:c?.querySelector('.companyName')?.textContent.trim()||'',salary:'',tags:a.innerText};
});
'''
JOB51_CARDS = '''
return Array.from(document.querySelectorAll('.joblist-item')).map(c=>{
 let d={};try{d=JSON.parse(c.querySelector('[sensorsdata]')?.getAttribute('sensorsdata')||'{}')}catch(e){}
 return {job_id:String(d.jobId||''),title:d.jobTitle||'',company:c.querySelector('.cname')?.innerText.trim()||'',
 salary:d.jobSalary||'',tags:[d.jobArea,d.jobYear,d.jobDegree].filter(Boolean).join(' '),
 href:'',button:c.querySelector('button.btn.apply')?.innerText.trim()||''};
});
'''
YUPAO_CARDS = '''
return Array.from(document.querySelectorAll('main [data-index]')).map(c=>{
 const a=c.querySelector('a[href*="/zhaogong/"]');
 return {href:a?.href||'',title:c.querySelector('.acccc')?.textContent.trim()||'',
 company:c.querySelector('a[href*="/qiye/"]')?.textContent.trim()||'',
 salary:c.querySelector('.accch')?.textContent.trim()||'',tags:c.querySelector('.accci')?.innerText||''};
});
'''


# 只有维护者根据真实首单证据添加精确端点及成功条件。
# 猎聘：2026-10-14 两次校准（1985442155、1975191697），open-chat 返回 flag=1 且按钮变“继续聊”。
VERIFIED_ENDPOINTS = {
    'liepin': {'https://api-c.liepin.com/api/com.liepin.im.c.chat.open-chat': ('flag', 1)},
}
CALIBRATED = {'liepin'}


class PlatformAdapter:
    verified_endpoints = {}
    calibrated = False
    success_texts = ('已投递', '已申请', '申请成功', '投递成功', '继续聊', '继续沟通', '简历已发送')

    def __init__(self, platform, browser, args, city_code):
        self.platform, self.browser, self.args, self.city_code = platform, browser, args, city_code
        self.domain = DOMAINS[platform]
        self.evidence_domains = EVIDENCE_DOMAINS[platform]
        self.verified_endpoints = VERIFIED_ENDPOINTS.get(platform, {})
        self.calibrated = platform in CALIBRATED
        self.search_tab = None
        self.detail_tab = None
        self.owned_details = {}
        self.current = None

    def request_id_aliases(self, job_id):
        """猎聘页面 ID = "19" + 接口 jobId（1 个真实样本：1975191697 → 75191697）。"""
        job_id = str(job_id)
        if self.platform == 'liepin' and len(job_id) == 10 and job_id.startswith('19'):
            return {job_id[2:]}
        return set()

    @property
    def action_tab(self):
        return self.search_tab if self.platform in ('51job', 'yupao') else self.detail_tab

    def search(self, page=1):
        url = build_search_url(self.platform, self.args.job, self.city_code, page)
        if self.search_tab is None:
            p = urlsplit(url)
            self.search_tab = pick_tab(self.browser.get_tabs(), p.hostname, p.path)
            if self.search_tab is None:
                self.search_tab = self.browser.new_tab(url)
            else:
                self.search_tab.get(url)
        else:
            self.search_tab.get(url)
        time.sleep(5)
        self.apply_filters()

    def apply_filters(self):
        """原生操作页面筛选；未知选项报错，不套用其他平台的数字代码。"""
        requested = [("工作类型", getattr(self.args, "job_type", "")),
                     ("工作年限", getattr(self.args, "experience", "")),
                     ("学历要求", getattr(self.args, "degree", ""))]
        if self.platform == "yupao" and any(value for _, value in requested):
            raise AdapterError("鱼泡无实习/学历/经验筛选，请用关键词（如 ‘AI产品实习’）并依赖 Jev 硬条件")
        for label, value in requested:
            if not value:
                continue
            if self.platform == "liepin" and label == "学历要求":
                if "," in value or "，" in value:
                    raise AdapterError("猎聘学历筛选只支持一个值")
                if value not in ("博士", "硕士", "本科", "大专", "中专/中技", "高中", "初中及以下"):
                    raise AdapterError("猎聘未找到学历选项：" + value)
            status, reason = self.check()
            if status:
                return  # runner.check 负责冷却与落盘
            if self.platform == "liepin" and label == "工作年限":
                option = self.search_tab.run_js('''
                    return Array.from(document.querySelectorAll('[data-key="workYearCode"][data-name]'))
                      .find(e=>e.getAttribute("data-name")===arguments[0])||null;
                ''', value)
                if not option:
                    raise AdapterError("猎聘未找到经验筛选选项：" + value)
                option.click(by_js=False)
            elif self.platform == "liepin" and label == "学历要求":
                control = self.search_tab.ele(
                    'xpath://span[contains(@class,"ant-select-selection-placeholder") and text()="学历"]'
                    '/ancestor::div[contains(@class,"ant-select-selector")][1]', timeout=1)
                if not control:
                    raise AdapterError("猎聘未找到学历选项：" + value)
                control.click(by_js=False)
                time.sleep(1.5)
                options = self.search_tab.eles(f'css:div.ant-select-item-option[title="{value}"]', timeout=1) or []
                option = next((e for e in options if e.states.has_rect), None)
                if option is None:
                    raise AdapterError("猎聘未找到学历选项：" + value)
                option.click(by_js=False)
            elif self.platform == "51job":
                if label == "工作年限" and value in ("在校生", "应届生", "在校生,应届生"):
                    value = "在校生/应届生"
                container = self.search_tab.run_js('''
                    const t=Array.from(document.querySelectorAll(".fixed-text"))
                      .find(e=>e.textContent.trim().startsWith(arguments[0]));
                    return t?.closest(".custom-select-container")||null;
                ''', label)
                if not container:
                    raise AdapterError("51job 未找到筛选控件：" + label)
                container.click(by_js=False)
                option = container.run_js('''
                    return Array.from(this.parentElement.querySelectorAll(".custom-option"))
                        .find(e=>e.textContent.trim()===arguments[0])||null;
                ''', value)
                if not option:
                    raise AdapterError("51job 未找到筛选选项：" + value)
                option.click(by_js=False)
            else:
                raise AdapterError(self.platform + " 的" + label + "尚未校准，不能静默忽略")
            time.sleep(3)
            if self.platform == "liepin" and label == "学历要求":
                selected = self.search_tab.run_js('''
                    return Array.from(document.querySelectorAll(".ant-select-selection-item"))
                      .some(e=>e.getAttribute("title")===arguments[0] || e.textContent.trim()===arguments[0]);
                ''', value)
                if not selected:
                    raise AdapterError("猎聘学历选中校验失败：" + value)

    def check(self):
        for tab in (self.search_tab, self.detail_tab):
            if tab is not None:
                reason = page_block_reason(tab)
                if reason:
                    return 'blocked', reason
                reason = page_gate_reason(tab)
                if reason:
                    return 'review', reason
        return '', ''

    def cards(self):
        script = {'liepin': LIEPIN_CARDS, '51job': JOB51_CARDS, 'yupao': YUPAO_CARDS}[self.platform]
        cards = self.search_tab.run_js(script) or []
        for c in cards:
            c['job_id'] = c.get('job_id') or parse_job_id(self.platform, c.get('href'))
            # 51job 卡片没有职位 href，不能把公司链接当职位链接。
            c['url'] = safe_url(c.get('href', ''))
        return cards

    def locate_card(self, card):
        if self.platform == '51job':
            return self.search_tab.run_js('''
                return Array.from(document.querySelectorAll('.joblist-item')).find(c=>{
                  try {const d=JSON.parse(c.querySelector('[sensorsdata]')?.getAttribute('sensorsdata')||'{}');
                  return String(d.jobId)===arguments[0] && d.jobTitle.trim()===arguments[1]}catch(e){return false}
                })||null;
            ''', card['job_id'], card['title'].strip())
        return self.search_tab.run_js('''
            const pattern=new RegExp(arguments[0]);
            return Array.from(document.querySelectorAll(arguments[3])).find(a=>{
                const m=new URL(a.href).pathname.match(pattern);return m&&m[1]===arguments[1];
            })||null;
        ''', PATTERNS[self.platform], card['job_id'], card['title'],
           'a[data-nick="job-detail-job-info"]' if self.platform == 'liepin' else 'main [data-index] a[href*="/zhaogong/"]')

    def open_detail(self, card):
        self.close_detail()
        self.current = card
        element = self.locate_card(card)
        if not element:
            return {}
        if self.platform == 'yupao':
            # 点击卡片容器切换右侧详情；点击标题链接会另开页面。
            element = element.run_js('return this.closest("[data-index]");')
        elif self.platform == '51job':
            element = element.ele('css:.jname', timeout=1)
        before = set(self.browser.tab_ids)
        try:
            element.click(by_js=False)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if self.platform != 'yupao':
                    for tab_id in set(self.browser.tab_ids) - before:
                        tab = self.browser.get_tab(tab_id)
                        if not self.opened_by_search(tab):
                            continue
                        self.owned_details[tab_id] = tab
                        if parse_job_id(self.platform, tab.url) == card['job_id']:
                            self.detail_tab = tab
                    if self.detail_tab is None and self.owned_details:
                        self.detail_tab = next(iter(self.owned_details.values()))
                status, reason = self.check()
                if status:
                    return {'gate': status, 'reason': reason}
                if self.detail_tab is not None:
                    host = urlsplit(self.detail_tab.url).hostname or ''
                    if host and not (host == self.domain or host.endswith('.' + self.domain)):
                        return {'external': True}
                result = self.detail_snapshot()
                if same_job(card, result) and result.get('jd'):
                    result['button_state'] = self.button_state(card)
                    result['button_ready'] = self.verified_button(card) is not None
                    return result
                time.sleep(.5)
            return self.detail_snapshot()
        finally:
            # 包括异常期间新开的 tab；只关闭本次标题点击所产生的标签。
            for tab_id in set(self.browser.tab_ids) - before:
                tab = self.browser.get_tab(tab_id)
                if self.opened_by_search(tab):
                    self.owned_details.setdefault(tab_id, tab)

    def opened_by_search(self, tab):
        # 不把同时由用户或其他任务打开的标签当成本轮资产。
        info = tab.run_cdp('Target.getTargetInfo', targetId=tab.tab_id)['targetInfo']
        return info.get('openerId') == self.search_tab.tab_id

    def detail_snapshot(self):
        tab = self.search_tab if self.platform == 'yupao' else self.detail_tab
        if tab is None:
            return {}
        if self.platform == 'yupao':
            data = tab.run_js('''
                const r=document.querySelector('#mainR');const text=s=>r?.querySelector(s)?.innerText.trim()||'';
                return {title:text('h1'),jd:text('.afghh'),company:text('.afgec')||text('.afiag'),
                  href:r?.querySelector('a[href*="/zhaogong/"]')?.href||''};
            ''') or {}
            data['job_id'] = parse_job_id('yupao', data.get('href', ''))
        else:
            data = tab.run_js('''
                const text=s=>document.querySelector(s)?.innerText.trim()||'';
                return {title:text(arguments[0]),jd:text(arguments[1]),company:text(arguments[2])};
            ''', '.jTitle h1' if self.platform == '51job' else '.job-apply-content .job-title',
               '.job_msg' if self.platform == '51job' else '.job-intro-container .paragraph,.job-intro-container',
               '.cname' if self.platform == '51job' else '.company-name') or {}
            data['job_id'] = parse_job_id(self.platform, tab.url)
            if self.platform == 'liepin':
                # .company-name 可落在推荐岗位，不能拿它补当前雇主。当前公司只取搜索卡片。
                data['company'] = ''
        data['url'] = safe_url(tab.url) if self.platform != 'yupao' else safe_url(data.get('href', ''))
        return data

    def _button(self, card):
        if not same_job(card, self.detail_snapshot()):
            return None
        if self.platform == '51job':
            c = self.locate_card(card)
            return c.ele('css:button.btn.apply', timeout=.2) if c else None
        root = '#mainR > div:first-child' if self.platform == 'yupao' else '.job-apply-container,.job-title,.job-summary'
        return self.action_tab.run_js('''
            const roots=Array.from(document.querySelectorAll(arguments[0]));
            const labels=JSON.parse(arguments[1]);
            const candidates=roots.flatMap(r=>Array.from(r.querySelectorAll('button,a,div')))
              .filter(e=>e.getClientRects().length&&labels.includes(e.innerText.trim())
                 && !Array.from(e.children).some(c=>labels.includes(c.innerText?.trim())));
            for(const label of labels){const b=candidates.find(e=>e.innerText.trim()===label);if(b)return b}return null;
        ''', root, json.dumps((['发送简历'] if self.platform == 'yupao' else ['聊一聊']) + list(self.success_texts)))

    def verified_button(self, card):
        b = self._button(card)
        if not b or b.attr('disabled') is not None or b.attr('aria-disabled') == 'true':
            return None
        if b.text.strip() not in ('投递', '申请职位', '聊一聊', '发送简历'):
            return None
        return b

    def button_state(self, card):
        b = self._button(card)
        return b.text.strip() if b else ''

    def next_page(self, number):
        if self.platform == 'yupao':
            old = {c['job_id'] for c in self.cards()}
            self.search_tab.run_js('window.scrollTo(0,document.body.scrollHeight)')
            time.sleep(5)
            return bool({c['job_id'] for c in self.cards()} - old)
        self.search(number)
        return True

    def close_detail(self):
        for tab in self.owned_details.values():
            try:
                tab.close()
            except Exception:
                pass
        self.owned_details.clear()
        self.detail_tab = None

    def close(self):
        self.close_detail()
