"""Boss JD × 简历的 Jev 匹配、离线决策、缓存与队列报告。"""

import hashlib
import json
import math
import os
import re
from datetime import datetime, timedelta
from http.client import HTTPException
from pathlib import Path
from urllib import error, request

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
HARD_SKIP_THRESHOLD = 0.2
HARD_APPLY_THRESHOLD = 0.6
FIT_SKIP_THRESHOLD = 1.5
FIT_APPLY_THRESHOLD = 2.3
ACTION_THRESHOLD = 0.6
TITLE_SKIP_THRESHOLD = 0.8
LATEST_MAX_AGE = timedelta(days=7)
MAX_CONSECUTIVE_FAILURES = 3
ACTION_LABELS = {"0": "投", "1": "复核", "2": "跳"}

TITLE_QUESTIONS = {
    "title_fit": {
        "type": "choice",
        "instructions": "仅根据岗位标题和标签，这个岗位是否可能是候选人想要的产品/AI产品方向实习？",
        "criteria": {"0": "可能相关", "1": "明显无关（如销售、剪辑、设计、编剧、标注、教练、客服、纯技术研发）"},
    },
}

# 硬条件拆成三问取最小值：合并成一问时概率被压低且不稳定（实测同一 JD 0.27/0.16），
# 拆开后能说清卡在哪一项。
HARD_QUESTIONS = {
    "edu": ("学历", "岗位对学历/学校层次（如985/211/QS排名/硕士）的明确要求，候选人是否满足？JD未提及视为满足。"),
    "stage": ("在校/应届", "岗位是否接受在校生实习或2027届应届生？若JD明确为社招全职或要求工作年限，则为否；未说明时按岗位标题判断。"),
    "schedule": ("到岗/时长/城市", "岗位对每周到岗天数、实习时长、工作城市的明确要求，候选人是否满足？JD未提及视为满足。"),
}

QUESTIONS = {
    **{k: {"type": "noul", "instructions": q} for k, (_, q) in HARD_QUESTIONS.items()},
    "fit": {
        "type": "score",
        "instructions": "候选人的项目经历与能力，与该岗位核心职责的匹配程度",
        "criteria": ["不相关或非产品岗（如销售、剪辑、纯运营）", "少量相关", "部分匹配", "高度匹配"],
    },
    "action": {
        "type": "choice",
        "instructions": "综合判断该岗位应如何处理",
        "criteria": {"0": "推荐投递", "1": "需要人工复核", "2": "跳过"},
    },
}


class JevError(Exception):
    """Jev 请求或响应不可用；错误消息不包含密钥、state 或响应正文。"""


def load_jev_key(key_file: Path | None = None) -> str:
    """环境变量优先；可注入文件路径，绝不输出凭证。"""
    key = os.environ.get("JEV_API_KEY", "").strip()
    if key:
        return key
    key_file = key_file or Path("~/.config/jobhunter/jev.env").expanduser()
    try:
        for line in Path(key_file).read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() == "JEV_API_KEY":
                return value.strip().strip("\"'")
    except (OSError, UnicodeError):
        pass
    return ""


def redact_resume(text: str, resume_redact: list[str] | tuple = ()) -> str:
    """纯函数：自定义隐去项由调用方从 config 显式传入。"""
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[已隐去]", text)
    text = re.sub(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+",
                  "[已隐去]", text)
    for item in sorted(set(resume_redact), key=len, reverse=True):
        if item:
            text = text.replace(item, "[已隐去]")
    return text


def clean_jd(text: str) -> str:
    """去掉 Boss 详情里的反爬水印字、页头和招聘者/地址页脚。"""
    text = re.sub(r"来自BOSS直聘|BOSS直聘|kanzhun|boss", "", text or "")
    text = re.sub(r"^(举报\s*)?(微信扫码分享\s*)?(职位描述\s*)?", "", text.strip())
    text = re.split(r"\n\s*\S+\s*刚刚活跃|\n\s*去App|\n\s*工作地址", text)[0]
    return text.strip()


def build_state(title, company, jd_text, resume_text, profile) -> str:
    return (f"【岗位】{title}｜{company}\n{jd_text}\n"
            f"【候选人简历】\n{redact_resume(resume_text)}\n"
            f"【求职偏好】\n{profile}")


def _probability(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError
    return value


def _distribution(values, allowed) -> dict:
    if not isinstance(values, dict) or not values or not values.keys() <= allowed:
        raise ValueError
    probs = {key: _probability(value) for key, value in values.items()}
    # 接受接口小数舍入，但不把缺失/损坏的分布当成推荐依据。
    if not math.isclose(sum(probs.values()), 1.0, abs_tol=0.02):
        raise ValueError
    return probs


def decide(answers: dict) -> dict:
    """按固定顺序分流；只用 probabilities，不把 noul 当满足证明。"""
    try:
        hard_detail = {k: _probability(answers[k]["noul"]) for k in HARD_QUESTIONS}
        weakest = min(hard_detail, key=hard_detail.get)
        hard_ok = hard_detail[weakest]
        weak_label = f"{HARD_QUESTIONS[weakest][0]} {hard_ok:.2f}"
        fit_probs = _distribution(answers["fit"]["probabilities"], {"0", "1", "2", "3"})
        fit_ev = sum(int(i) * p for i, p in fit_probs.items())
        action = answers["action"]["choice"]
        action_probs = _distribution(answers["action"]["probabilities"], {"0", "1", "2"})
        action_p = action_probs[action]
    except (KeyError, TypeError, ValueError, AttributeError):
        raise JevError("Jev 响应格式错误") from None

    if hard_ok < HARD_SKIP_THRESHOLD:
        decision, reason = "skip", f"硬条件冲突（{weak_label}）"
    elif action == "2" and action_p >= ACTION_THRESHOLD:
        decision, reason = "skip", "Jev 建议跳过"
    elif fit_ev < FIT_SKIP_THRESHOLD:
        decision, reason = "skip", "方向不匹配"
    # 方案 B：前面已挡掉冲突/跳过/不对口；这里不再要求 action="投" 且高概率
    # （实测 Jev 很少给出 p≥0.6 的"推荐投递"），只要没被建议跳过即可
    elif (hard_ok >= HARD_APPLY_THRESHOLD and fit_ev >= FIT_APPLY_THRESHOLD
          and action != "2"):
        decision, reason = "apply", "匹配达到推荐阈值"
    else:
        reasons = []
        if hard_ok < HARD_APPLY_THRESHOLD:
            reasons.append(f"硬条件不确定（{weak_label}）")
        if fit_ev < FIT_APPLY_THRESHOLD:
            reasons.append(f"能力匹配不足 {fit_ev:.2f}")
        if action == "2":
            reasons.append(f"Jev 倾向跳过 p={action_p:.2f}")
        decision, reason = "review", "；".join(reasons)
    return {"decision": decision, "reason": reason, "hard_ok": hard_ok, "hard_detail": hard_detail,
            "fit_ev": fit_ev, "action": action, "action_p": action_p}


def decide_title(answer: dict) -> tuple[bool, str]:
    """只有明确选无关且概率达到阈值才拦截；格式错误交由调用方放行。"""
    try:
        if answer.get("type", "choice") != "choice":
            raise ValueError
        choice = answer["choice"]
        probs = _distribution(answer["probabilities"], {"0", "1"})
        probability = probs[choice]
    except (KeyError, TypeError, ValueError, AttributeError):
        raise JevError("Jev 标题响应格式错误") from None
    skip = choice == "1" and probability >= TITLE_SKIP_THRESHOLD
    return skip, f"标题粗筛无关 p={probability:.2f}" if skip else ""


def _request_jev(state: str, key: str, questions: dict, timeout) -> dict:
    payload = {"model": JEV_MODEL, "state": state, "questions": questions}
    req = request.Request(
        JEV_URL, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read())
        return data["answers"]
    except error.HTTPError as exc:
        raise JevError(f"Jev HTTP 错误 {exc.code}") from None
    except (OSError, HTTPException, ValueError, TypeError, KeyError):
        raise JevError("Jev 网络或解析失败") from None


def call_jev(state: str, key: str, timeout=30) -> dict:
    answers = _request_jev(state, key, QUESTIONS, timeout)
    decide(answers)  # 不完整/非数值回答与解析错误一样进入人工复核。
    return answers


def call_title_jev(state: str, key: str, timeout=30) -> dict:
    answers = _request_jev(state, key, TITLE_QUESTIONS, timeout)
    try:
        answer = answers["title_fit"]
    except (KeyError, TypeError):
        raise JevError("Jev 标题响应格式错误") from None
    decide_title(answer)
    return answer


def cache_key(job_id: str, jd_text: str) -> str:
    digest = hashlib.sha1(jd_text.encode("utf-8")).hexdigest()[:12]
    return f"boss:{job_id}:{digest}"


def _load_cache(cache_file: Path) -> dict:
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
        return cache if isinstance(cache, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict, cache_file: Path):
    temp = cache_file.with_suffix(".json.tmp")
    temp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(cache_file)


def latest_match(job_id: str, cache_file: Path, now: datetime | None = None) -> dict | None:
    """仅返回 7 天内完整的最新决策；读取不延长缓存有效期。"""
    cached = _load_cache(cache_file).get(f"latest:{job_id}")
    try:
        age = (now or datetime.now().astimezone()) - datetime.fromisoformat(cached["ts"])
        decision = cached["decision"]
        if (timedelta(0) <= age < LATEST_MAX_AGE
                and decision["decision"] in ("apply", "skip", "review")
                and isinstance(decision["reason"], str)):
            return cached
    except (KeyError, TypeError, ValueError):
        pass
    return None


def match_title(job_id, state, key, cache_file: Path, *, refresh=False) -> tuple[bool, str]:
    cache = _load_cache(cache_file)
    entry_key = f"title:{job_id}"
    cached = cache.get(entry_key)
    if not refresh and isinstance(cached, dict) and "answer" in cached:
        try:
            return decide_title(cached["answer"])
        except JevError:
            pass
    answer = call_title_jev(state, key)
    result = decide_title(answer)
    cache[entry_key] = {"answer": answer, "ts": datetime.now().astimezone().isoformat()}
    _save_cache(cache, cache_file)
    return result


def match_job(job_id, jd_text, state, key, cache_file: Path, *, refresh=False) -> dict:
    """缓存仅保存 answers/decision/ts，不保存简历、JD、state 或密钥。"""
    cache = _load_cache(cache_file)
    entry_key = cache_key(job_id, jd_text)
    cached = cache.get(entry_key)
    result = None
    if not refresh and isinstance(cached, dict) and "answers" in cached:
        try:
            # 阈值调整后仍用缓存 answers 按当前规则决策。
            result = {**cached, "decision": decide(cached["answers"])}
        except JevError:
            pass
    if result is None:
        answers = call_jev(state, key)
        result = {"answers": answers, "decision": decide(answers)}
    now = datetime.now().astimezone().isoformat()
    result.setdefault("ts", now)
    cache[entry_key] = result
    # 只有实际读过 JD 才续期；旧答案可复用，但索引记录本次确认 JD 的时间。
    cache[f"latest:{job_id}"] = {"decision": result["decision"], "ts": now}
    _save_cache(cache, cache_file)
    return result


def format_result(result: dict) -> str:
    return (f"hard={result['hard_ok']:.2f} fit={result['fit_ev']:.2f} "
            f"action={ACTION_LABELS[result['action']]} p={result['action_p']:.2f}")


def render_queue_md(rows: list[dict], stats: dict) -> str:
    """本次队列的纯渲染函数；不读文件、不修改 rows/stats。"""
    def cell(value):
        return str(value if value is not None else "—").replace("&", "&amp;").replace(
            "<", "&lt;").replace(">", "&gt;").replace("|", "&#124;").replace(
            "\r", " ").replace("\n", "<br>")

    def table(headers, data):
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join("---" for _ in headers) + " |")
        for values in data:
            lines.append("| " + " | ".join(cell(value) for value in values) + " |")
        if not data:
            lines.append("\n暂无。")
        lines.append("")

    def detail(row):
        result = row.get("jev", {}).get("decision", {})
        return [row.get("company", ""), row.get("job", ""), row.get("salary", ""),
                f"{result['hard_ok']:.2f}" if "hard_ok" in result else "—",
                f"{result['fit_ev']:.2f}" if "fit_ev" in result else "—",
                ACTION_LABELS.get(result.get("action"), "—"),
                row.get("reason", ""), row.get("url", "")]

    lines = [f"# Boss {cell(stats.get('city', ''))} 待投队列", "", "## 本次统计", "",
             "｜".join(f"{label}：{stats.get(key, 0)}" for key, label in (
                 ("apply", "推荐"), ("review", "复核"), ("skip", "跳过"),
                 ("applied", "已沟通"), ("pending", "待核验"))), "",
             "推荐/复核/跳过为筛选结论；已沟通/待核验为沟通结果，数量不相加。", ""]
    if stats.get("stop_reason"):
        lines.extend([f"停止原因：{cell(stats['stop_reason'])}", ""])
    headers = ["公司", "岗位", "薪资", "hard", "fit", "Jev 建议", "原因", "链接"]
    for heading, selected in (
        ("需要人工复核", [r for r in rows if r.get("decision") == "review"]),
        ("推荐但未投", [r for r in rows if r.get("decision") == "apply"
                   and not r.get("attempted") and r.get("status") != "already_contacted"]),
    ):
        lines.extend([f"## {heading}", ""])
        table(headers, [detail(row) for row in selected])
    lines.extend(["## 本次跳过", ""])
    skipped = [r for r in rows if r.get("decision") == "skip"
               or r.get("status") == "already_contacted"]
    table(["公司", "岗位", "原因"], [[r.get("company", ""), r.get("job", ""),
                                    r.get("reason", "")] for r in skipped[:50]])
    if len(skipped) > 50:
        lines.extend([f"仅展示前 50 行，共 {len(skipped)} 行。", ""])
    if stats.get("dry_run"):
        lines.extend(["## keyword 对照", "", "— 表示在读取 JD 前拦截或读取失败，未计算旧分数。", ""])
        table(["公司", "岗位", "匹配结论", "旧 score_jd 分数"], [
            [r.get("company", ""), r.get("job", ""), r.get("decision", "—"),
             r.get("keyword_score", "—")] for r in rows])
    return "\n".join(lines)
