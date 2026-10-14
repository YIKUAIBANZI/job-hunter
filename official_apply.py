"""从各平台对口岗位整理官网、JD 邮箱及手动表单；不提交投递。"""

import argparse
import copy
import html
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from urllib import error, request
from urllib.parse import unquote, urlsplit

from jev_match import JEV_MODEL, JEV_URL, JevError, load_jev_key
from shared import cache_path, extract_emails, log_path, report_path

TAVILY_URL = "https://api.tavily.com/search"
AGGREGATORS = [
    "zhipin.com", "kanzhun.com", "liepin.com", "51job.com", "zhaopin.com",
    "lagou.com", "shixiseng.com", "yingjiesheng.com", "maimai.cn", "jobui.com",
    "58.com", "ganji.com", "baidu.com/zhaopin", "zhihu.com", "xiaohongshu.com",
    "weibo.com", "sohu.com", "163.com", "new.qq.com", "csdn.net",
    "niuqizp.com",  # 第三方校招聚合，campus. 子域会被误判为官方 ATS
]
ATS_DOMAINS = (
    "mokahr.com", "zhiye.com", "hotjob.cn", "jobs.feishu.cn", "feishu.cn/hire",
    "hr.163.com", "careers.tencent.com", "join.qq.com", "jobs.bytedance.com",
)
RECRUIT_SUBDOMAIN = re.compile(r"^(campus|career|careers|job|jobs|hr|zhaopin|talent|join)\.")
RECRUIT_PATH = re.compile(r"career|careers|job|jobs|zhaopin|recruit|campus|join")
KIND_RANK = {"ATS/招聘子站": 2, "招聘页": 1, "其他": 0}
CACHE_TTL = timedelta(days=30)
MAX_CANDIDATES = 6
MAX_PAGE_BYTES = 200 * 1024
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
PLATFORM_LABELS = {'boss': 'boss', 'liepin': '猎聘', 'yingjiesheng': '应届生',
                   '51job': '51job', 'yupao': '鱼泡'}


class SearchError(Exception):
    """只携带固定错误信息，不携带密钥、请求或接口响应正文。"""


def normalize_company(company: str) -> str:
    return "".join(company.split()).lower()


def _log_records(directory):
    """只读统一日志目录；坏日志和未知平台不影响其他公司。"""
    for path in sorted(log_path(directory, 'boss', '').parent.glob('*-log.json')):
        platform = path.name.split('-', 1)[0]
        if platform not in PLATFORM_LABELS:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for bucket, rows in data.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                yield platform, bucket, row


def collect_companies(directory: Path) -> list[dict]:
    """合并各平台 applied/review；skipped/failed 不因 decision=apply 入选。"""
    companies = {}
    for platform, bucket, row in _log_records(directory):
        if bucket not in ('applied', 'review'):
            continue
        company = row.get('company')
        if not isinstance(company, str) or not normalize_company(company):
            continue
        entry = companies.setdefault(normalize_company(company), {
            'company': company.strip(), 'jobs': [], 'sources': [], 'emails': [], 'boss_urls': [],
        })
        job = row.get('job') if isinstance(row.get('job'), str) else ''
        url = row.get('url') if isinstance(row.get('url'), str) else ''
        job, url = job.strip(), url.strip()
        if job and job not in entry['jobs']:
            entry['jobs'].append(job)
        source = dict(platform=platform, job=job, url=url, bucket=bucket)
        if source not in entry['sources']:
            entry['sources'].append(source)
        if platform == 'boss' and url and url not in entry['boss_urls']:
            entry['boss_urls'].append(url)
        for value in row.get('emails', []) if isinstance(row.get('emails'), list) else []:
            for email in extract_emails(value if isinstance(value, str) else ''):
                if email not in entry['emails']:
                    entry['emails'].append(email)
    return list(companies.values())


def collect_manual_forms(directory: Path) -> list[dict]:
    """应届生按 ID 取最新记录；已投过的岗位不再提示手动表单。"""
    latest, applied = {}, set()
    for platform, bucket, row in _log_records(directory):
        if platform != 'yingjiesheng':
            continue
        job_id = str(row.get('job_id') or row.get('jobId') or '')
        if not re.fullmatch(r'[0-9]+', job_id):
            continue
        if bucket == 'applied':
            applied.add(job_id)
        try:
            stamp = datetime.fromisoformat(row.get('ts', ''))
            stamp = stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).timestamp()
        except (ValueError, TypeError, OverflowError):
            stamp = float('-inf')
        if job_id not in latest or stamp >= latest[job_id][0]:
            latest[job_id] = (stamp, bucket, {**row, 'job_id': job_id})
    return [row for _, bucket, row in sorted(latest.values(), key=lambda item: item[0], reverse=True)
            if row.get('manual_form') is True and bucket != 'applied' and row['job_id'] not in applied]


def load_tavily_key(key_file: Path | None = None) -> str:
    """环境变量优先；配置文件位于仓库之外，密钥不进入日志或缓存。"""
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if key:
        return key
    key_file = key_file or Path("~/.config/jobhunter/search.env").expanduser()
    try:
        for line in Path(key_file).read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() == "TAVILY_API_KEY":
                return value.strip().strip("\"'")
    except (OSError, UnicodeError):
        pass
    return ""


def _url_parts(url):
    """只接收可直接请求的原始 HTTP(S) URL，不补协议、不拼接域名。"""
    if not isinstance(url, str) or re.search(r"[\s<>\\\x00-\x1f\x7f]", url):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
            return None
        parts.port  # 拒绝非法端口。
        return parts
    except ValueError:
        return None


def _matches_rule(parts, rule: str) -> bool:
    host, _, path = rule.partition("/")
    actual_host = parts.hostname.lower().rstrip(".")
    if not (actual_host == host or actual_host.endswith("." + host)):
        return False
    return not path or unquote(parts.path).lower().startswith("/" + path)


def is_aggregator(url: str) -> bool:
    parts = _url_parts(url)
    if parts is None:
        return True
    # hr.163.com 是明确列出的 ATS，不能被 163.com 的新闻站规则误杀。
    if any(_matches_rule(parts, domain) for domain in ATS_DOMAINS):
        return False
    if any(_matches_rule(parts, domain) for domain in AGGREGATORS):
        return True
    return (parts.hostname.lower().rstrip(".") == "zhaopin.baidu.com"
            or parts.hostname.lower().endswith(".zhaopin.baidu.com"))


def classify_kind(url: str) -> str:
    parts = _url_parts(url)
    if parts is None:
        return "其他"
    if (any(_matches_rule(parts, domain) for domain in ATS_DOMAINS)
            or RECRUIT_SUBDOMAIN.match(parts.hostname.lower())):
        return "ATS/招聘子站"
    if RECRUIT_PATH.search(unquote(parts.path).lower()):
        return "招聘页"
    return "其他"


def tavily_search(query: str, key: str, timeout=30) -> list[dict]:
    """Tavily 鉴权和请求结构集中于此；按官方文档使用 Bearer 请求头。"""
    payload = {"query": query, "max_results": 8, "search_depth": "basic",
               "include_answer": False, "exclude_domains": AGGREGATORS}
    req = request.Request(
        TAVILY_URL, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read())
        results = data["results"]
        if not isinstance(results, list):
            raise ValueError
        return results
    except error.HTTPError as exc:
        raise SearchError(f"Tavily HTTP 错误 {exc.code}") from None
    except (OSError, HTTPException, ValueError, TypeError, KeyError):
        raise SearchError("Tavily 网络或解析失败") from None


def build_queries(company: str, jobs: list[str]) -> list[str]:
    return [f"{company} 校园招聘 官网 实习", f"{company} {jobs[0] if jobs else ''} 招聘".replace("  ", " ")]


def _probability(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError
    return float(value)


def parse_jev_answers(answers: dict) -> dict:
    """只保留参与判断的数值，不使用 choice confidence 代替官方选项概率。"""
    try:
        official = answers["official"]
        choice = official["choice"]
        probabilities = official["probabilities"]
        if set(probabilities) != {"0", "1", "2"} or choice not in probabilities:
            raise ValueError
        probs = {k: _probability(v) for k, v in probabilities.items()}
        if not math.isclose(sum(probs.values()), 1, abs_tol=0.02):
            raise ValueError
        return {"official_choice": choice, "official": probs["0"],
                "job_match": _probability(answers["job_match"]["noul"])}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise JevError("Jev 响应格式错误") from None


def call_jev(company: str, job: str, candidate: dict, key: str, timeout=30) -> dict:
    questions = {
        "official": {
            "type": "choice",
            "instructions": f"该网页是否为『{company}』官方的招聘/校招页面或其官方使用的招聘系统页面？",
            "criteria": {"0": "是该公司官方招聘入口", "1": "第三方聚合/新闻/无关页面", "2": "是其他公司的页面"},
        },
        "job_match": {
            "type": "noul",
            "instructions": f"该页面是否包含『{job}』或高度相似的具体职位？",
        },
    }
    state = (f"【公司】{company}\n【Boss岗位】{job}\n【网页标题】{candidate['title']}\n"
             f"【URL】{candidate['url']}\n【摘要】{candidate['content'][:1500]}")
    payload = {"model": JEV_MODEL, "state": state, "questions": questions}
    req = request.Request(
        JEV_URL, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read())
        return parse_jev_answers(data["answers"])
    except error.HTTPError as exc:
        raise JevError(f"Jev HTTP 错误 {exc.code}") from None
    except (OSError, HTTPException, ValueError, TypeError, KeyError):
        raise JevError("Jev 网络或解析失败") from None


def check_reachable(url: str) -> tuple[int | None, str]:
    """一次只读 GET；最多读取 200 KB，状态以 urllib 返回的最终响应为准。"""
    if _url_parts(url) is None:
        return None, "URL 格式不可请求"
    req = request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    try:
        with request.urlopen(req, timeout=8) as response:
            status = response.getcode()
            response.read(MAX_PAGE_BYTES)
        return status, "" if 200 <= status < 400 else f"HTTP {status}，未确认可达"
    except error.HTTPError as exc:
        # urllib 会将未跟随的重定向也作为 HTTPError 抛出。
        return exc.code, "" if 200 <= exc.code < 400 else f"HTTP {exc.code}，未确认可达"
    except (OSError, HTTPException, ValueError):
        return None, "网络、超时或 TLS 错误，未确认可达"


def candidate_status(candidate: dict) -> str:
    if candidate["official_choice"] != "0" or candidate["official"] < 0.4:
        return "未找到"
    status = candidate.get("http_status")
    if candidate["official"] >= 0.6 and isinstance(status, int) and 200 <= status < 400:
        return "已核验"
    return "待核验"


def select_candidates(candidates: list[dict]) -> list[dict]:
    """先概率、再入口类型、再岗位命中；最多两个，原始 URL 原样保留。"""
    ordered = sorted(candidates, key=lambda c: (
        c["official"], KIND_RANK.get(c["kind"], 0),
        c["job_match"] if c.get("job_match") is not None else -1,
    ), reverse=True)
    selected, seen = [], set()
    for candidate in ordered:
        status = candidate_status(candidate)
        if status == "未找到" or candidate["url"] in seen:
            continue
        seen.add(candidate["url"])
        selected.append({**candidate, "status": status})
        if len(selected) == 2:
            break
    return selected


def company_status(candidates: list[dict]) -> str:
    statuses = {candidate_status(candidate) for candidate in candidates}
    return "已核验" if "已核验" in statuses else "待核验" if "待核验" in statuses else "未找到"


def _redact(text: str, secrets) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[已隐去]")
    return text


def find_official(company: dict, tavily_key: str, jev_key: str, now: datetime) -> dict:
    queries = build_queries(company["company"], company["jobs"])
    found, seen, errors = [], set(), []
    for query in queries:
        try:
            rows = tavily_search(query, tavily_key)
        except SearchError as exc:
            errors.append(str(exc))
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = row.get("url")
            if is_aggregator(url) or url in seen:
                continue
            if any(key and key in url for key in (tavily_key, jev_key)):
                continue  # 不改写 URL；意外回显凭证的结果直接丢弃。
            seen.add(url)
            found.append({"url": url, **{
                field: _redact(row.get(field) or "", (tavily_key, jev_key))
                if isinstance(row.get(field), str) else ""
                for field in ("title", "content")
            }})
    candidates = []
    job = company["jobs"][0] if company["jobs"] else ""
    for raw in found[:MAX_CANDIDATES]:
        try:
            judgement = call_jev(company["company"], job, raw, jev_key)
        except JevError as exc:
            errors.append(str(exc))
            continue
        candidate = {"url": raw["url"], "title": raw["title"], "kind": classify_kind(raw["url"]),
                     **judgement, "http_status": None, "checked_at": now.isoformat(), "reason": ""}
        if not job:
            candidate["job_match"] = None
        if judgement["official_choice"] == "0" and judgement["official"] >= 0.6:
            candidate["http_status"], candidate["reason"] = check_reachable(raw["url"])
        elif judgement["official_choice"] == "0" and judgement["official"] >= 0.4:
            candidate["reason"] = f"官方概率 {judgement['official']:.2f}，低于 0.60，尚未核验可达性"
        candidates.append(candidate)
    selected = select_candidates(candidates)
    return {**company, "queries": queries, "matched_job": job, "candidates": selected,
            "source_urls": [raw["url"] for raw in found[:MAX_CANDIDATES]],
            "status": company_status(selected), "errors": list(dict.fromkeys(errors)), "cached": False}


def load_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_cached(cache: dict, company: dict, now: datetime) -> dict | None:
    """30 天内复用官网证据；岗位变化时不沿用旧岗位命中结论。"""
    entry = cache.get(normalize_company(company["company"]))
    try:
        age = now - datetime.fromisoformat(entry["ts"])
        if not timedelta(0) <= age < CACHE_TTL:
            return None
        result = copy.deepcopy(entry["result"])
        if (normalize_company(result["company"]) != normalize_company(company["company"])
                or result["errors"] or not isinstance(result["queries"], list)
                or not isinstance(result["source_urls"], list) or not isinstance(result["candidates"], list)):
            return None
        for candidate in result["candidates"]:
            if candidate["url"] not in result["source_urls"] or is_aggregator(candidate["url"]):
                return None
            _probability(candidate["official"])
            if candidate["official_choice"] not in ("0", "1", "2"):
                return None
            if candidate["job_match"] is not None:
                _probability(candidate["job_match"])
            if not all(isinstance(candidate[k], str) for k in ("title", "kind", "checked_at", "reason")):
                return None
        result["candidates"] = select_candidates(result["candidates"])
        job = company["jobs"][0] if company["jobs"] else ""
        if result["matched_job"] != job:
            for candidate in result["candidates"]:
                candidate["job_match"] = None
        result.update(company)
        result.update(cached=True, status=company_status(result["candidates"]))
        return result
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _cell(value) -> str:
    text = html.escape(str(value if value is not None else "—"), quote=False)
    # 防止标题/公司中的 Markdown 或 HTML 注入额外链接。
    for char in "\\`[]!*_|":
        text = text.replace(char, f"&#{ord(char)};")
    return text.replace("\r", " ").replace("\n", " ")


def job_match_label(value) -> str:
    if value is None:
        return "不确定"
    return "是" if value >= 0.6 else "否" if value <= 0.4 else "不确定"


def _link(url, title):
    if _url_parts(url) is None:
        return '—'
    target = html.escape(url, quote=False).replace('|', '&#124;')
    return f'[{_cell(title)}](<{target}>)'


def _job_labels(result):
    sources = result.get('sources') or []
    jobs = list(dict.fromkeys(f"{s['job']}（{PLATFORM_LABELS.get(s['platform'], s['platform'])}）"
                             for s in sources if s.get('job')))
    if not jobs:
        jobs = list(result.get('jobs', []))
    return '、'.join(jobs[:3]) + (f'等 {len(jobs)} 个' if len(jobs) > 3 else '') or '—'


def render_official_md(results: list[dict], now: datetime, manual_forms=None) -> str:
    """纯渲染；链接只能取候选的原始 URL，不从公司名或标题生成链接。"""
    counts = {status: sum(r["status"] == status for r in results) for status in ("已核验", "待核验", "未找到")}
    cached = sum(bool(r.get("cached")) for r in results)
    searched = sum(bool(r.get('searched', not r.get('cached'))) for r in results)
    websites = sum(bool(r['candidates']) for r in results)
    emails = sum(bool(r.get('emails')) for r in results)
    neither = sum(not r['candidates'] and not r.get('emails') for r in results)
    lines = ["# 官网投递", "", f"生成时间：{now.isoformat(timespec='seconds')}", "",
             f"公司数：{len(results)}｜有官网入口：{websites}｜有投递邮箱：{emails}｜两者皆无：{neither}", "",
             f"查了 {len(results)} 家（本轮搜索 {searched} 家，缓存 {cached} 家）；"
             + "｜".join(f"{status}：{count}" for status, count in counts.items()), "",
             "官网链接均来自搜索结果，投递前请人工确认岗位仍开放。", "",
             "统计按公司计数；同公司可以有两个入口，分别列入对应状态表。"
             "岗位命中仅判断首个岗位；缓存沿用原核验时间，首个岗位变化则记为不确定。", "",
             "邮件投递建议标题：姓名-学校-岗位-到岗时间（请按 JD 要求）", "",
             "## 官网/邮箱清单", "",
             "| 公司 | 对口岗位（平台） | 官网入口 | 投递邮箱 | 平台链接 |",
             "|---|---|---|---|---|"]
    for result in sorted(results, key=lambda r: not (r.get('emails') or r['status'] == '已核验')):
        entrances = '、'.join(_link(c['url'], (c.get('title') or '招聘入口')[:30])
                             + f"（{_cell(c['status'])}）" for c in result['candidates']) or '—'
        sources = result.get('sources') or []
        urls = result.get('boss_urls') or []
        url = sources[0].get('url', '') if sources else urls[0] if urls else ''
        cells = [_cell(result['company']), _cell(_job_labels(result)), entrances,
                 _cell('、'.join(result.get('emails', [])) or '—'), _link(url, '岗位')]
        lines.append('| ' + ' | '.join(cells) + ' |')
    lines.append('')
    headers = ["公司", "对口岗位", "官方入口", "类型", "岗位命中", "HTTP", "核验时间"]
    for status in ("已核验", "待核验"):
        table_headers = headers + (["原因"] if status == "待核验" else [])
        lines.extend([f"## {status}", "", "| " + " | ".join(table_headers) + " |",
                      "| " + " | ".join("---" for _ in table_headers) + " |"])
        for result in results:
            for candidate in result["candidates"]:
                if candidate["status"] != status:
                    continue
                url = candidate["url"]
                if _url_parts(url) is None:
                    continue
                # 尖括号目的地址支持 URL 内括号；实体编码仅转义 Markdown 表格字符。
                target = html.escape(url, quote=False).replace("|", "&#124;")
                title = _cell(candidate["title"][:30] or "招聘入口")
                cells = [_cell(result["company"]), _cell("、".join(result["jobs"]) or "—"),
                         f"[{title}](<{target}>)", _cell(candidate["kind"]),
                         job_match_label(candidate["job_match"]), _cell(candidate["http_status"]),
                         _cell(candidate["checked_at"])]
                if status == "待核验":
                    cells.append(_cell(candidate["reason"]))
                lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    lines.extend(["## 未找到", ""])
    for result in results:
        if result["status"] == "未找到":
            suffix = "（查询未完成，见异常说明）" if result.get("errors") else ""
            lines.append(f"- {_cell(result['company'])} — 已尝试的查询："
                         + "；".join(_cell(q) for q in result["queries"]) + suffix)
    failures = [r for r in results if r.get("errors")]
    if failures:
        lines.extend(["", "## 查询异常", "", "以下结果不完整，未写入本次缓存，可重新运行：", ""])
        lines.extend(f"- {_cell(r['company'])}：{_cell('；'.join(r['errors']))}" for r in failures)
    lines.extend(['', '## 平台表单待手动投递（应届生）', '',
                  '| 公司 | 岗位 | Jev 结论(hard/fit) | 链接 |', '|---|---|---|---|'])
    for row in manual_forms or []:
        d = (row.get('jev') or {}).get('decision') or {}
        metric = lambda value: f'{value:.2f}' if isinstance(value, (int, float)) else '—'
        conclusion = f"{d.get('decision', '—')}（{metric(d.get('hard_ok'))}/{metric(d.get('fit_ev'))}）"
        lines.append('| ' + ' | '.join([_cell(row.get('company', '')), _cell(row.get('job', '')),
                                        _cell(conclusion), _link(row.get('url', ''), '岗位')]) + ' |')
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="从全平台对口岗位整理官网/邮箱清单（只读，不自动投递）")
    parser.add_argument("--company", help="只查询指定公司")
    parser.add_argument("--job", help="与 --company 配合使用的岗位名")
    parser.add_argument('--emails-only', action='store_true', help='仅输出日志邮箱、手动表单及 30 天内已缓存官网，不调用 Tavily/Jev')
    parser.add_argument("--limit", type=int, default=20, help="本轮最多新查询的公司数，缓存命中不占额度（默认 20）")
    parser.add_argument("--refresh", action="store_true", help="忽略 30 天缓存，强制重查")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit 必须大于 0")
    if args.job is not None and args.company is None:
        parser.error("--job 需要与 --company 一起使用")
    if args.company is not None and not normalize_company(args.company):
        parser.error("--company 不能为空")
    tavily_key = '' if args.emails_only else load_tavily_key()
    emails_only = args.emails_only or not tavily_key
    if not tavily_key and not args.emails_only:
        print('缺少 TAVILY_API_KEY，自动降级为 --emails-only：仅整理日志邮箱和已缓存官网。', file=sys.stderr)
    jev_key = '' if emails_only else load_jev_key()
    if not emails_only and not jev_key:
        print("缺少 JEV_API_KEY：请设置环境变量或 ~/.config/jobhunter/jev.env。", file=sys.stderr)
        return 1
    directory = Path(__file__).resolve().parent
    companies = collect_companies(directory)
    manual_forms = collect_manual_forms(directory)
    if args.company is not None:
        companies = [c for c in companies if normalize_company(c['company']) == normalize_company(args.company)] or [
            dict(company=args.company.strip(), jobs=[], sources=[], emails=[], boss_urls=[])]
        if args.job and args.job.strip():
            companies[0]['jobs'] = [args.job.strip()]
        manual_forms = [r for r in manual_forms if normalize_company(r.get('company', '')) == normalize_company(args.company)]
    now = datetime.now().astimezone()
    cache_file = cache_path(directory, "official-cache.json")
    cache = load_cache(cache_file)
    results, searched, skipped = [], 0, 0
    for company in companies:
        result = None if args.refresh and not emails_only else get_cached(cache, company, now)
        if result is None and emails_only:
            result = {**company, 'queries': [], 'candidates': [], 'status': '未找到',
                      'errors': [], 'cached': False, 'searched': False}
        if result is None:
            if searched >= args.limit:
                skipped += 1
                continue
            searched += 1
            result = find_official(company, tavily_key, jev_key, now)
            if not result["errors"]:
                cache[normalize_company(company["company"])] = {"ts": now.isoformat(), "result": result}
        results.append(result)
    try:
        # 即使上游意外回显 key，也不能落盘；URL 含 key 的候选已在搜索层丢弃。
        secrets = (tavily_key, jev_key)
        safe_cache = json.loads(_redact(json.dumps(cache, ensure_ascii=False), secrets))
        if not emails_only:
            save_cache(cache_file, safe_cache)
        path = report_path(directory, "官网投递.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_redact(render_official_md(results, now, manual_forms), secrets), encoding="utf-8")
    except (OSError, ValueError):
        print("无法写入官网报告或缓存，请检查目录权限和文件状态。", file=sys.stderr)
        return 1
    print(f"已生成 官网投递.md：{len(results)} 家，本轮搜索 {searched} 家，缓存 {sum(bool(r.get('cached')) for r in results)} 家。")
    if skipped:
        print(f"另有 {skipped} 家达到本轮额度，尚未查询；再次运行可继续。")
    if any(result["errors"] for result in results):
        print("部分查询失败，详见报告中的查询异常；失败结果未缓存。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
