"""各平台投递脚本公共模块"""

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from DrissionPage import ChromiumOptions, ChromiumPage

BOSS_JOB_ID_PATTERN = r"(?:^|/)job_detail/([A-Za-z0-9_-]+)\.html$"

DEFAULT_CONFIG = {
    "resume_path": "",
    "job_profile": "",
    "resume_redact": [],
    "greeting": "您好，我对贵司岗位非常感兴趣，期待进一步沟通！",
    "skills": [],
    "target_roles": [],
    "exclude_keywords": ["总监", "架构师", "首席", "VP", "P8", "P7"],
    "boost_keywords": ["llm", "大模型", "agent", "rag", "gpt", "ai产品", "人工智能"],
    "company_blacklist": [],
    "min_score": 60,
    "default_count": 20,
}


def load_config(skill_dir: Path | None = None) -> dict:
    """读取用户配置。优先 config.json，缺省字段用 DEFAULT_CONFIG 补齐。"""
    skill_dir = skill_dir or Path(__file__).parent
    cfg_file = skill_dir / "config.json"
    cfg = dict(DEFAULT_CONFIG)
    if cfg_file.exists():
        cfg.update(json.loads(cfg_file.read_text(encoding="utf-8")))
    return cfg


def load_log(log_file: Path) -> dict:
    log = json.loads(log_file.read_text()) if log_file.exists() else {}
    for bucket in ("applied", "pending", "review", "skipped", "failed"):
        log.setdefault(bucket, [])
    return log


def save_log(log: dict, log_file: Path):
    log_file.write_text(json.dumps(log, ensure_ascii=False, indent=2))


def parse_boss_job_id(href: str) -> str:
    """从相对/绝对职位链接的路径提取 encryptJobId，忽略 query 和 fragment。"""
    if not isinstance(href, str):
        return ""
    try:
        url = urlsplit(href)
    except ValueError:
        return ""
    if url.scheme not in ("", "http", "https"):
        return ""
    match = re.search(BOSS_JOB_ID_PATTERN, url.path)
    return match.group(1) if match else ""


def classify_boss_chat_response(packet_info: dict | None) -> tuple[str, str]:
    """按原始 http_status/code/message 判定；截断 message 只在日志层进行。"""
    if not packet_info:
        return "pending", "未捕获接口响应"
    code = packet_info.get("code")
    message = str(packet_info.get("message") or "")
    if packet_info.get("http_status") == 200 and code == 0:
        return "chat_started", "接口确认"
    if any(word in message for word in ("上限", "频繁", "验证", "异常")):
        return "blocked", message
    return "failed", f"code={code} {message}"


def score_jd(title: str, desc: str, config: dict | None = None) -> tuple[int, str]:
    """基于关键词的快速评分。

    config 字段：
      - exclude_keywords: 命中任一则归零
      - target_roles:     命中标题 +30
      - skills:           每命中一个 JD +5，封顶 +30
      - boost_keywords:   JD 命中任一 +10
    """
    cfg = config or DEFAULT_CONFIG
    title_lower = title.lower()
    combined = title_lower + " " + desc.lower()

    for kw in cfg.get("exclude_keywords", []):
        if kw.lower() in combined:
            return 0, f"包含排除词: {kw}"

    score = 0
    hits = []

    for role in cfg.get("target_roles", []):
        if role.lower() in title_lower:
            score += 30
            hits.append(f"目标岗位:{role}")
            break

    if any(kw in combined for kw in ["实习", "校招", "应届", "intern"]):
        score += 30
        hits.append("接受实习/应届")

    skill_matches = [s for s in cfg.get("skills", []) if s.lower() in combined]
    if skill_matches:
        bonus = min(len(skill_matches) * 5, 30)
        score += bonus
        hits.append(f"技能匹配:{'/'.join(skill_matches[:3])}")

    for kw in cfg.get("boost_keywords", []):
        if kw.lower() in combined:
            score += 10
            hits.append(kw)
            break

    return min(score, 100), "、".join(hits) if hits else "无匹配"


# ---------- 浏览器连接 ----------


def connect_chrome(port: int = 9222) -> ChromiumPage:
    """CDP 连接本地已启动的 Chrome（--remote-debugging-port）。"""
    opts = ChromiumOptions().set_local_port(port)
    return ChromiumPage(addr_or_opts=opts)


# ---------- 参数解析 ----------


def parse_common_args(
    city_codes: dict,
    *,
    default_city: str = "全国",
    default_count: int = 20,
    default_min_score: int = 60,
    count_help: str = "投递数量",
    include_matcher: bool = False,
    argv: list[str] | None = None,
) -> argparse.Namespace:
    """四个平台通用参数；--dry-run / --fast 目前仅 Boss 实际使用。

    差异（默认城市、默认数量、count 帮助文案）通过关键字参数注入，
    argv 仅供测试注入，正常运行传 None 走 sys.argv。
    """
    p = argparse.ArgumentParser()
    p.add_argument("--job", required=True, help="搜索岗位名")
    p.add_argument(
        "--city", default=default_city, help=f"城市，可选: {list(city_codes)}"
    )
    p.add_argument("--count", type=int, default=default_count, help=count_help)
    p.add_argument(
        "--min-score",
        type=int,
        default=default_min_score,
        help="最低评分（0=不过滤）",
    )
    p.add_argument("--dry-run", action="store_true", help="仅 Boss：只读 JD 和打分，不沟通")
    p.add_argument("--fast", action="store_true", help="仅 Boss 调试：沟通间隔降至 3–6 秒")
    if include_matcher:
        p.add_argument("--matcher", choices=("jev", "keyword"), default="jev",
                       help="Boss 匹配器：jev（默认）或 keyword（使用 --min-score）")
    return p.parse_args(argv)


# ---------- 日志写入 ----------


def record(log: dict, log_file: Path, bucket: str, entry: dict) -> dict:
    """把一条记录追加到 log[bucket]（applied/pending/review/skipped/failed）并落盘。"""
    log[bucket].append(entry)
    save_log(log, log_file)
    return log


def print_summary(
    platform: str, applied: int, skipped: int, log_file: Path, pending: int = 0
):
    """统一的结尾汇总框。"""
    print(f"""
╔══════════════════════════════════╗
  {platform} 投递完成
  成功: {applied} 份  待核验: {pending} 份  跳过: {skipped} 份
  日志: {log_file}
╚══════════════════════════════════╝
""")


# ---------- 跨运行去重 + 公司黑名单 ----------


def _norm(text) -> str:
    """归一化：去首尾/合并内部空白 + 转小写。"""
    return " ".join(str(text or "").split()).lower()


def dedup_key(company, title) -> str:
    """去重键 = 归一化(公司)||归一化(标题)。

    公司缺失时退化为按标题去重；公司与标题都为空时返回空串（调用方忽略空键）。
    """
    c = _norm(company)
    t = _norm(title)
    if not c and not t:
        return ""
    return f"{c}||{t}"


def load_applied_index(skill_dir: Path | None = None) -> set[str]:
    """聚合 *-log.json 的 applied/pending；保留公司+标题键，职位 ID 另加 boss: 键。

    跨搜索、跨平台生效；删除某个 log 文件即自动"遗忘"对应记录。
    坏文件/缺字段一律跳过，不抛异常。
    """
    skill_dir = skill_dir or Path(__file__).parent
    index: set[str] = set()
    for log_file in skill_dir.glob("*-log.json"):
        try:
            data = json.loads(log_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for bucket in ("applied", "pending"):
            entries = data.get(bucket, [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                key = dedup_key(entry.get("company", ""), entry.get("job", ""))
                if key:
                    index.add(key)
                if entry.get("job_id"):
                    index.add(f"boss:{entry['job_id']}")
    return index


def is_blacklisted(company, cfg: dict) -> bool:
    """公司名子串命中 config.company_blacklist（大小写不敏感）即拉黑。"""
    c = _norm(company)
    if not c:
        return False
    return any(_norm(b) in c for b in cfg.get("company_blacklist", []) if b)
