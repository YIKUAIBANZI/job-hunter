"""各平台投递脚本公共模块"""

import json
from pathlib import Path

DEFAULT_CONFIG = {
    "resume_path": "",
    "greeting": "您好，我对贵司岗位非常感兴趣，期待进一步沟通！",
    "skills": [],
    "target_roles": [],
    "exclude_keywords": ["总监", "架构师", "首席", "VP", "P8", "P7"],
    "boost_keywords": ["llm", "大模型", "agent", "rag", "gpt", "ai产品", "人工智能"],
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
    if log_file.exists():
        return json.loads(log_file.read_text())
    return {"applied": [], "skipped": [], "failed": []}


def save_log(log: dict, log_file: Path):
    log_file.write_text(json.dumps(log, ensure_ascii=False, indent=2))


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
