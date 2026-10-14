"""51job、猎聘、鱼泡的统一评估/提交流程，Boss 保留已验证的入口。"""
import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import jev_match as jev
from apply_runtime import (CHAT_INTERVAL, SUCCESS_REST, SUCCESS_REST_EVERY,
                           PLATFORM_PACING,
                           paced_rest, submit, write_cooldown)
from platform_adapters import AdapterError, PlatformAdapter, same_job, parse_job_id
from review_list import approved_review_rows
from shared import (connect_chrome, cooldown_remaining, dedup_key, is_blacklisted,
                    load_applied_index, load_config, load_log, record, save_log, score_jd)


class StopRun(Exception):
    pass


def parse_args(city_codes, argv=None, *, platform='liepin', default_count=20, default_min_score=60):
    p = argparse.ArgumentParser(description='点击 + 核验：默认 Jev；先运行 --dry-run')
    p.add_argument('--job', default='', help='搜索岗位名（--approved 时不需要）')
    p.add_argument('--approved', action='store_true', help='只投递复核清单中本平台的人工批准岗位，不搜索、不调 Jev')
    p.add_argument('--job-id', default='', help='仅处理指定职位 ID；首单授权时锁定岗位')
    p.add_argument('--city', default='深圳', choices=list(city_codes))
    p.add_argument('--count', type=int, default=default_count, help='真投成功目标；dry-run 最多评估岗位数')
    p.add_argument('--dry-run', action='store_true', help='只读和评估，绝不提交')
    p.add_argument('--matcher', choices=('jev', 'keyword'), default='jev')
    p.add_argument('--min-score', type=int, default=default_min_score)
    p.add_argument('--match-policy', choices=('review-gate', 'boss-b'), default='review-gate',
                   help='默认 Jev 建议复核不自动投；boss-b 保留交接方案 B')
    p.add_argument('--max-views', type=int, default=PLATFORM_PACING[platform]['max_views'],
                   help='最多读取详情数；51job 默认 20，其他平台默认 60')
    p.add_argument('--max-pages', type=int, default=5)
    p.add_argument('--job-type', default='', help='平台工作类型选项，51job 支持；默认不限')
    p.add_argument('--experience', default='', help='平台经验选项：猎聘 实习生/应届生；51job 在校生/应届生')
    p.add_argument('--degree', default='', help='猎聘/51job 学历选项，如 本科；鱼泡不支持')
    p.add_argument('--refresh', action='store_true')
    p.add_argument('--fill-queue', action='store_true')
    p.add_argument('--calibrate', action='store_true', help='用户授权后仅真投 1 次，采集首单核验证据')
    args = p.parse_args(argv)
    if args.approved and platform == '51job':
        p.error('51job 暂不支持批准投递，请在页面手动处理')
    if not args.job and not args.approved:
        p.error('必须提供 --job，或使用 --approved')
    if args.approved and args.fill_queue:
        p.error('--approved 不支持 --fill-queue')
    if not 0 <= args.min_score <= 100:
        p.error('min-score 为 0–100')
    if args.count < 1 or not 1 <= args.max_views <= 60 or not 1 <= args.max_pages <= 20:
        p.error('count 必须为正；max-views 为 1–60；max-pages 为 1–20')
    if args.job_id and not re.fullmatch(r'[0-9]+', args.job_id):
        p.error('job-id 必须为平台数字 ID')
    if args.calibrate and not args.job_id:
        p.error('首单校准必须 --job-id 锁定用户确认的具体岗位')
    if args.calibrate and (args.count != 1 or args.fill_queue):
        p.error('首单校准必须 --count 1，不能 --fill-queue')
    return args


def cache_scope(platform, cfg, resume, profile):
    # 缓存随简历、偏好、问题/阈值版本变化失效，缓存里不写个人原文。
    payload = json.dumps([resume, profile, cfg.get('exclude_keywords'),
                          jev.QUESTIONS, jev.TITLE_QUESTIONS, jev.JEV_MODEL,
                          [jev.HARD_SKIP_THRESHOLD, jev.HARD_APPLY_THRESHOLD, jev.FIT_SKIP_THRESHOLD,
                           jev.FIT_APPLY_THRESHOLD, jev.ACTION_THRESHOLD, jev.TITLE_SKIP_THRESHOLD],
                          'multi-platform-v1'], ensure_ascii=False, sort_keys=True)
    return platform + ':' + hashlib.sha256(payload.encode()).hexdigest()[:16]


def effective_decision(result, policy):
    decision = dict(result['decision'])
    if policy == 'review-gate' and decision.get('action') == '1' and decision['decision'] == 'apply':
        decision.update(decision='review', reason='Jev 建议人工复核，未进入自动投递')
    return decision


def execute(platform, args, cfg, adapter, root, *, rest=paced_rest):
    pacing = PLATFORM_PACING[platform]
    log_file = root / f'{platform}-{args.city}-log.json'
    queue_file = root / f'{platform}-{args.city}-队列.md'
    cooldown_file = root / f'{platform}-cooldown.json'
    log = load_log(log_file)
    index = load_applied_index(root)
    rows, seen = [], set()
    counts = dict(applied=0, pending=0, views=0, evaluated=0)
    stop_reason = ''
    failures = 0
    key = resume = profile = ''
    cache_file = root / f'jev-{platform}-cache.json'
    current = {'platform': platform}

    def persist(bucket, entry):
        record(log, log_file, bucket, {**entry, 'ts': datetime.now().astimezone().isoformat()})

    def check():
        nonlocal stop_reason
        status, reason = adapter.check()
        if status:
            if status == 'blocked':
                write_cooldown(cooldown_file, reason)
                persist('failed', {**current, 'status': 'blocked', 'reason': reason})
            stop_reason = reason
            raise StopRun(reason)

    def decision(entry, name, reason):
        entry.update(decision=name, reason=reason)
        if name in ('review', 'skip'):
            persist('review' if name == 'review' else 'skipped', entry)
        print(f"[{name}] {entry.get('company','')} | {entry.get('job','')} | ID={entry.get('job_id','')} | {reason}")

    try:
        remaining = cooldown_remaining(cooldown_file, datetime.now().astimezone())
        if remaining:
            raise StopRun(f'风控冷却中，剩余 {int(remaining.total_seconds()/60)+1} 分钟；未连接浏览器')
        if args.approved and platform == '51job':
            raise StopRun('51job 暂不支持批准投递，请在页面手动处理')
        if not args.dry_run and not adapter.calibrated and not args.calibrate:
            raise StopRun('该平台尚未首单校准；请先 dry-run，用户授权后 --calibrate --count 1')
        approved_cards = []
        if args.approved:
            approved_cards = [{**r, 'title': r.get('job', ''), 'review_entry': r}
                              for r in approved_review_rows(root, platform, args.city)]
            if not approved_cards:
                raise StopRun('没有本城市可投递的人工批准岗位，未连接浏览器')
        if not args.approved and args.matcher == 'jev':
            key = jev.load_jev_key()
            if not key:
                raise StopRun('缺少 JEV_API_KEY，不降级、不连接浏览器')
            path = Path(cfg.get('resume_path', '')).expanduser()
            if not path.is_absolute():
                path = root / path
            try:
                resume = jev.redact_resume(path.read_text(encoding='utf-8'), cfg['resume_redact'])
                profile = jev.redact_resume(cfg.get('job_profile', ''), cfg['resume_redact'])
                if not resume.strip():
                    raise ValueError('空简历')
            except (OSError, UnicodeError, ValueError):
                raise StopRun('简历不可读：请配置 UTF-8 文本简历') from None
        scope = '' if args.approved else cache_scope(platform, cfg, resume, profile)
        if not args.approved:
            adapter.search()
        check()
        page = 1
        while page <= args.max_pages:
            check()
            cards = approved_cards if args.approved else adapter.cards()
            fresh = [c for c in cards if (c['job_id'] or ('missing', c['title'])) not in seen]
            if not cards:
                raise StopRun('没有读取到职位卡片：可能未登录、列表为空或页面结构变化')
            if not fresh:
                raise StopRun('列表未出现新职位，停止重复翻页')
            for card in fresh:
                if args.job_id and card['job_id'] != args.job_id:
                    seen.add(card['job_id'] or ('missing', card['title']))
                    continue
                if args.dry_run and counts['evaluated'] >= args.count:
                    raise StopRun('已达 dry-run 评估数量')
                if not args.dry_run and counts['applied'] >= args.count and not args.fill_queue:
                    raise StopRun('已达投递目标')
                check()
                seen.add(card['job_id'] or ('missing', card['title']))
                current = entry = {**card.get('review_entry', {}),
                                  'platform': platform, 'job_id': card['job_id'], 'job': card['title'],
                                  'company': card.get('company', ''), 'salary': card.get('salary', ''),
                                  'url': card.get('url', '')}
                rows.append(entry)
                id_key = f"{platform}:{card['job_id']}"
                company_key = dedup_key(entry['company'], entry['job']) if entry['company'] else ''
                if not card['job_id'] or not card['title']:
                    decision(entry, 'skip', '无稳定职位 ID 或标题')
                    continue
                if id_key in index or company_key and company_key in index:
                    decision(entry, 'skip', '已投递或待核验（跨运行去重）')
                    continue
                if is_blacklisted(entry['company'], cfg):
                    decision(entry, 'skip', '公司黑名单')
                    continue
                if args.approved and parse_job_id(platform, entry['url']) != card['job_id']:
                    decision(entry, 'review', '日志详情 URL 与批准岗位 ID 不匹配')
                    continue
                counts['evaluated'] += 1
                cache_id = scope + ':' + card['job_id']
                try:
                    cached = None if args.approved or args.refresh or args.matcher != 'jev' else jev.latest_match(cache_id, cache_file)
                    if cached:
                        result = cached
                        entry['cache_hit'] = True
                    else:
                        if not args.approved and args.matcher == 'jev':
                            excluded = next((kw for kw in cfg['exclude_keywords'] if kw and kw.lower() in card['title'].lower()), '')
                            if excluded:
                                decision(entry, 'skip', f'标题排除词：{excluded}')
                                continue
                            title_state = jev.build_state(card['title'], entry['company'], card.get('tags', ''), '', profile)
                            reject, why = (False, '') if jev.title_protected(card['title']) else \
                                jev.match_title(cache_id, title_state, key, cache_file, refresh=args.refresh)
                            if reject:
                                failures = 0
                                decision(entry, 'skip', why)
                                continue
                        result = None
                    # 缓存 apply 真投前仍需打开并核验当前详情，缓存 review/skip 不重开。
                    need_detail = result is None or (not args.dry_run and effective_decision(result, args.match_policy)['decision'] == 'apply'
                                                     and counts['applied'] < args.count)
                    if need_detail:
                        if counts['views'] >= args.max_views:
                            decision(entry, 'review', '已达详情上限，尚未评估')
                            raise StopRun('已达 --max-views 上限')
                        counts['views'] += 1
                        detail = adapter.open_approved_detail(card) if args.approved else adapter.open_detail(card)
                        check()
                        rest(pacing['view_interval'], check)
                        if counts['views'] % pacing['view_rest_every'] == 0:
                            rest(pacing['view_rest'], check)
                        if detail.get('external'):
                            decision(entry, 'skip', '跳转到外部招聘平台，不在本次投递范围')
                            continue
                        if not same_job(card, detail) or not detail.get('jd'):
                            decision(entry, 'review', '卡片与详情 ID/标题不匹配，或 JD 为空')
                            continue
                        entry['url'] = detail.get('url') or entry['url']
                        entry['detail_verified'] = True
                        entry['button_ready'] = detail.get('button_ready')
                        entry['button_state'] = detail.get('button_state', '')
                        entry['detail_title'] = detail['title']
                        entry['jd_chars'] = len(detail['jd'])
                        if not entry['company']:
                            entry['company'] = detail.get('company', '')
                            card['company'] = entry['company']
                        company_key = dedup_key(entry['company'], entry['job']) if entry['company'] else ''
                        if is_blacklisted(entry['company'], cfg):
                            decision(entry, 'skip', '详情公司黑名单')
                            continue
                        if company_key and company_key in index:
                            decision(entry, 'skip', '详情公司与已投岗位重复')
                            continue
                        if args.approved:
                            result = {'decision': {'decision': 'apply', 'reason': '人工批准复核岗位'}}
                        else:
                            jd = jev.clean_jd(card.get('tags', '') + '\n' + detail['jd'])
                            score, why = score_jd(card['title'], jd, cfg)
                            entry['keyword_score'] = score
                            if args.matcher == 'jev':
                                state = jev.build_state(card['title'], entry['company'], jd, resume, profile)
                                result = jev.match_job(cache_id, jd, state, key, cache_file, refresh=args.refresh)
                            else:
                                result = {'decision': {'decision': 'apply' if score >= args.min_score else 'skip', 'reason': why}}
                    failures = 0
                    if not args.approved:
                        entry['jev'] = result if args.matcher == 'jev' else {}
                    d = effective_decision(result, args.match_policy)
                    decision(entry, d['decision'], d['reason'])
                    if args.dry_run or d['decision'] != 'apply' or counts['applied'] >= args.count:
                        continue
                    if not entry['company']:
                        decision(entry, 'review', '公司身份缺失，无法检查黑名单及跨平台去重')
                        continue
                    check()
                    attempt = {**entry, 'status': 'pending', 'attempted': True,
                               'reason': '提交前预记，异常退出时禁止重投'}

                    def before_click():
                        persist('pending', attempt)
                        index.add(id_key)
                        if company_key:
                            index.add(company_key)
                        entry['attempted'] = True
                        entry['status'] = 'pending'
                        counts['pending'] += 1

                    status, reason, evidence = submit(adapter, card, before_click, dry_run=args.dry_run)
                    if args.approved and status == 'applied':
                        reason = '[人工批准]' + reason
                    entry.update(status=status, reason=reason, evidence=evidence)
                    if evidence['attempted']:
                        # 替换预记；一次原子写入同时完成移出 pending 与写入最终状态。
                        log['pending'] = [e for e in log['pending'] if not (e.get('platform') == platform and e.get('job_id') == card['job_id'])]
                        bucket = 'applied' if status == 'applied' else 'pending'
                        log[bucket].append({**entry, 'ts': datetime.now().astimezone().isoformat()})
                        save_log(log, log_file)
                        if bucket == 'applied':
                            counts['pending'] -= 1
                            counts['applied'] += 1
                    else:
                        persist('skipped' if status == 'skipped' else 'review', entry)
                    if status == 'blocked':
                        write_cooldown(cooldown_file, reason)
                        persist('failed', {**entry, 'status': 'blocked'})
                        raise StopRun(reason)
                    if status == 'pending':
                        raise StopRun('本次提交待核验，停止并保留去重记录')
                    if evidence['attempted']:
                        if args.calibrate:
                            raise StopRun('首单校准已尝试一次，请检查双证据；未自动开启批量投递')
                        rest(CHAT_INTERVAL, check)
                        if status == 'applied' and counts['applied'] % SUCCESS_REST_EVERY == 0:
                            rest(SUCCESS_REST, check)
                except jev.JevError:
                    failures += 1
                    decision(entry, 'review', 'Jev 调用或响应解析失败')
                    if failures >= jev.MAX_CONSECUTIVE_FAILURES:
                        raise StopRun('Jev 连续失败 3 次')
                finally:
                    adapter.close_detail()
            if args.approved or args.fill_queue and counts['applied'] >= args.count:
                break
            page += 1
            if page > args.max_pages:
                stop_reason = '已达搜索页上限'
                break
            if not adapter.next_page(page):
                stop_reason = '没有加载到新职位'
                break
    except (StopRun, AdapterError) as exc:
        stop_reason = str(exc)
    except Exception as exc:
        # 不输出异常全文：第三方异常可能包含凭证或访问 URL。
        stop_reason = f'运行中断：{type(exc).__name__}；已保留日志'
        persist('failed', {**current, 'reason': stop_reason})
    finally:
        if args.job_id and not counts['evaluated'] and not stop_reason:
            stop_reason = '未找到指定职位 ID，未提交'
        adapter.close()
        stats = {**counts, 'platform': platform, 'city': args.city, 'dry_run': args.dry_run,
                 'stop_reason': stop_reason,
                 **{name: sum(r.get('decision') == name for r in rows) for name in ('apply', 'review', 'skip')}}
        queue_file.write_text(jev.render_queue_md(rows, stats), encoding='utf-8')
        print(f"{platform}：成功 {counts['applied']}，待核验 {counts['pending']}，详情 {counts['views']}，评估 {counts['evaluated']}")
        print(f'停止原因：{stop_reason or "本轮完成"}\n队列：{queue_file}')
    return stats


def main(platform, city_codes, argv=None):
    cfg = load_config()
    args = parse_args(city_codes, argv, platform=platform,
                      default_count=cfg['default_count'], default_min_score=cfg['min_score'])
    # 延迟连接：冷却、配置和校准门禁检查在 execute 里先于 search 完成。
    class LazyAdapter(PlatformAdapter):
        def open_approved_detail(self, card):
            if self.browser is None:
                self.browser = connect_chrome()
            return super().open_approved_detail(card)

        def search(self, page=1):
            if self.browser is None:
                self.browser = connect_chrome()
            super().search(page)
    adapter = LazyAdapter(platform, None, args, city_codes[args.city])
    return execute(platform, args, cfg, adapter, Path(__file__).parent)
