#!/usr/bin/env python3
"""供用户的 agent 调用的非交互配置 CLI；不保存密钥、不打开浏览器。"""

import argparse
import json
import os
import re
import socket
import tempfile
from copy import deepcopy
from pathlib import Path

import shared
import llm_match
from jev_match import DEFAULT_PROFILE_TEXT, load_jev_key
from shared import DEFAULT_CONFIG, effective_config

# 字段｜含义｜示例｜用户可能会这么说；点路径代替整段 JSON 输入。
FIELD_INFO = {
    "resume_path": ("UTF-8 txt/md 简历路径", "./resume.md", "换这份简历"),
    "resume_redact": ("外发前隐去的姓名等词", "张三，学校姓名", "隐去我的姓名"),
    "city": ("投递入口默认城市", "深圳", "换到杭州找工作"),
    "target_roles": ("搜索白名单与目标岗位关键词", "运营，电商运营", "方向改成运营"),
    "job_profile": ("身份、方向、城市与到岗偏好", "2027届本科，深圳，产品实习，每周5天", "我毕业了，改找社招"),
    "matcher": ("匹配后端 jev/llm/keyword", "llm", "换便宜模型"),
    "llm.base_url": ("OpenAI 兼容 API 根地址（含版本路径）", "https://api.example.com/v1", "换模型服务商"),
    "llm.model": ("服务商提供的模型名", "便宜模型的实际ID", "用 flash 或 mini 档"),
    "llm.api_key_env": ("存放密钥的环境变量名，不是密钥", "JOBHUNTER_LLM_KEY", "用另一个密钥环境变量"),
    "jev_direction": ("标题粗筛的目标方向，Jev/LLM 共用", "新媒体/电商运营岗位", "改找运营岗位"),
    "jev_title_unrelated": ("标题明显无关的岗位示例", "销售、纯技术研发", "不要研发岗"),
    "jev_stage_question": ("是否符合在校/届别/社招身份的提问", "岗位是否接受2027届在校生实习？", "我改找社招了"),
    "jev_fit_unrelated": ("能力匹配最低档的描述", "不相关或非运营岗", "按运营经历匹配"),
    "greeting": ("打招呼语", "您好，期待进一步沟通！", "换一句招呼语"),
    "greeting_template": ("可选补发招呼语模板", "您好，想了解{job}。{greeting}", "补发时带上岗位名"),
    "skills": ("关键词匹配的技能", "Python，数据分析", "加上数据分析技能"),
    "exclude_keywords": ("命中则排除的词", "总监，首席", "排除高级岗位"),
    "boost_keywords": ("关键词打分加分词", "电商，增长", "优先增长岗位"),
    "company_blacklist": ("公司名包含则跳过", "外包，劳务派遣", "不要外包公司"),
    "min_score": ("keyword 最低分，0–100", "60", "筛选严格一点"),
    "default_count": ("单入口默认份数，至少1；每日配额另设 daily.json", "20", "一次先看10份"),
}
LIST_FIELDS = {"target_roles", "skills", "exclude_keywords", "boost_keywords", "company_blacklist", "resume_redact"}
REQUIRED_FIELDS = {"resume_path", "target_roles", "job_profile"}


class ConfigError(ValueError):
    """可直接向用户显示的字段错误，不包含输入值或解析正文。"""


def _known(field):
    if field not in FIELD_INFO:
        raise ConfigError("未知字段；可用字段：" + "、".join(FIELD_INFO))


def _validate(field, value):
    _known(field)
    if field in LIST_FIELDS:
        if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
            raise ConfigError(f"{field} 必须为字符串列表")
    elif field in ("min_score", "default_count"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{field} 必须为整数")
        if (field == "min_score" and not 0 <= value <= 100) or (field == "default_count" and value < 1):
            raise ConfigError(f"{field} 超出范围（min_score 0–100，default_count ≥1）")
    elif not isinstance(value, str):
        raise ConfigError(f"{field} 必须为字符串")
    elif field == "matcher" and value not in ("jev", "llm", "keyword"):
        raise ConfigError("matcher 必须为 jev / llm / keyword")
    elif field == "llm.api_key_env" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ConfigError("llm.api_key_env 必须是环境变量名，不能是密钥")
    elif field == "llm.base_url" and value and not llm_match.valid_base_url(value):
        raise ConfigError("llm.base_url 必须为 http(s) API 地址，不能内嵌凭证、查询或片段")
    elif field == "city" and not value.strip():
        raise ConfigError("city 不能为空")


def parse_value(field, raw):
    """纯函数：解析自然语言映射后的字符串，不回显用户输入。"""
    _known(field)
    if field in LIST_FIELDS:
        value = [v.strip() for v in re.split("[,，]", raw) if v.strip()]
    elif field in ("min_score", "default_count"):
        try:
            value = int(raw)
        except ValueError:
            raise ConfigError(f"{field} 必须为整数") from None
    else:
        value = raw.strip()
    _validate(field, value)
    return value


def set_field(cfg, path, value):
    """纯函数：返回副本，保留未修改字段，拒绝任意密钥字段。"""
    _validate(path, value)
    updated = deepcopy(cfg)
    if "." in path:
        parent, child = path.split(".")
        if not isinstance(updated.get(parent, {}), dict):
            raise ConfigError(f"{parent} 必须为对象")
        updated.setdefault(parent, {})[child] = deepcopy(value)
    else:
        updated[path] = deepcopy(value)
    return updated


def unset_field(cfg, path):
    """删除可选字段，下一次读取回到默认值。"""
    _known(path)
    if path in REQUIRED_FIELDS:
        raise ConfigError(f"{path} 是必填字段，请使用 set")
    updated = deepcopy(cfg)
    if "." in path:
        parent, child = path.split(".")
        if not isinstance(updated.get(parent, {}), dict):
            raise ConfigError(f"{parent} 必须为对象")
        updated.get(parent, {}).pop(child, None)
    else:
        updated.pop(path, None)
    return updated


def probe_chrome_port():
    """只做本机 TCP 连接检测，不连接 CDP 或读取登录态。"""
    try:
        with socket.create_connection(("127.0.0.1", 9222), timeout=1):
            return True
    except OSError:
        return False


def check_config(cfg, root, *, probe_chrome):
    """逐项返回检查结果；探测函数可注入，None/False 跳过 Chrome。"""
    cfg = effective_config(cfg)
    rows = []

    def add(field, ok, message, warn=False):
        # warn：不算失败，但提示用户确认（如仍在用作者默认画像）
        rows.append({"field": field, "ok": bool(ok), "message": message, "warn": warn})

    try:
        path = Path(cfg["resume_path"]).expanduser()
        if not path.is_absolute():
            path = Path(root) / path
        readable = bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError, TypeError, ValueError):
        readable = False
    add("resume_path", readable, "简历文件可读且非空" if readable else "请配置可读且非空的 UTF-8 txt/md 简历")
    roles = cfg["target_roles"]
    roles_ok = isinstance(roles, list) and bool(roles) and all(isinstance(v, str) and v.strip() for v in roles)
    add("target_roles", roles_ok, "target_roles 非空" if roles_ok else "target_roles 必须为非空岗位列表")
    profile_ok = isinstance(cfg["job_profile"], str) and bool(cfg["job_profile"].strip())
    add("job_profile", profile_ok, "job_profile 非空" if profile_ok else "请填写身份、方向和到岗偏好 job_profile")
    # 其余字段结构也检查，避免 set 后留下无法供入口使用的配置。
    for field in FIELD_INFO:
        if field in REQUIRED_FIELDS or field in DEFAULT_PROFILE_TEXT:
            continue
        parent, sep, child = field.partition(".")
        try:
            value = cfg[parent][child] if sep else cfg[parent]
            _validate(field, value)
        except KeyError:
            continue  # greeting_template 等可选字段没有默认值。
        except (ValueError, TypeError):
            add(field, False, f"{field} 格式或范围无效；请查 fields")
    matcher = cfg["matcher"]
    if matcher == "jev":
        ready = bool(load_jev_key())
        message = "Jev 密钥已配置" if ready else "缺少 JEV_API_KEY（环境变量或 ~/.config/jobhunter/jev.env）"
    elif matcher == "llm":
        settings = cfg["llm"]
        ready = (isinstance(settings, dict) and llm_match.settings_ready(settings)
                 and bool(llm_match.load_llm_key(settings)))
        message = "LLM 接口、模型及密钥已配置" if ready else "LLM 需要 base_url、model 与密钥（环境变量或 ~/.config/jobhunter/llm.env）"
    else:
        ready = matcher == "keyword"
        message = "keyword 无需模型凭证" if ready else "matcher 必须为 jev / llm / keyword"
    add("matcher", ready, message)
    for field, default in DEFAULT_PROFILE_TEXT.items():
        value = cfg[field]
        ok = isinstance(value, str) and bool(value.strip())
        is_default = not ok or value == default
        message = "仍为默认 2027 届 AI 产品画像，若方向不同请设置" if is_default else "已自定义，Jev/LLM 共用"
        add(field, ok, f"{field}：{message}", warn=is_default)
    if probe_chrome:
        ok = (probe_chrome if callable(probe_chrome) else probe_chrome_port)()
        add("chrome", ok, "Chrome 9222 可连接（请自行确认平台已登录）" if ok else "Chrome 9222 不可连接，请启动并登录平台")
    else:
        add("chrome", True, "Chrome 9222 检测已跳过")
    return rows


def atomic_write(path, cfg):
    """同目录临时文件 + replace，写失败不留下半份配置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".config-", suffix=".tmp", delete=False) as handle:
            temp = Path(handle.name)
            json.dump(cfg, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _read_config(path):
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ConfigError("配置必须为 JSON 对象")
    return cfg


def print_check(cfg, root, *, probe_chrome=probe_chrome_port):
    rows = check_config(cfg, root, probe_chrome=probe_chrome)
    for row in rows:
        print(f'{"❌" if not row["ok"] else "⚠️" if row.get("warn") else "✅"} {row["message"]}')
    return int(any(not row["ok"] for row in rows))


def show_config(cfg):
    """只展示工具支持的配置字段，未知 api_key 等字段不回显。"""
    cfg = effective_config(cfg)
    shown = {k: cfg[k] for k in FIELD_INFO if "." not in k and k in cfg}
    settings = cfg["llm"] if isinstance(cfg["llm"], dict) else {}
    shown["llm"] = {k: settings.get(k, v) for k, v in llm_match.DEFAULT_SETTINGS.items()}
    shown["llm"]["密钥"] = "已配置" if llm_match.load_llm_key(settings) else "未配置"
    return shown


def main(argv=None, *, root=None):
    root = Path(root) if root is not None else shared.PROJECT_ROOT
    parser = argparse.ArgumentParser(description="供 agent 使用的非交互配置工具；密钥只放环境变量或仓库外 env 文件")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="从 config.example.json 初始化，绝不覆盖")
    init.add_argument("--resume", required=True)
    init.add_argument("--city", default="深圳")
    init.add_argument("--matcher", choices=("jev", "llm", "keyword"), default="jev")
    sub.add_parser("show", help="展示有效配置与密钥状态")
    setting = sub.add_parser("set", help="修改字段或 llm 点路径，并自动检查")
    setting.add_argument("field")
    setting.add_argument("value")
    unset = sub.add_parser("unset", help="删除可选字段，回到默认")
    unset.add_argument("field")
    check = sub.add_parser("check", help="检查配置与 Chrome TCP 端口")
    check.add_argument("--no-chrome", action="store_true", help="跳过 Chrome 9222 TCP 探测")
    sub.add_parser("fields", help="打印自然语言到字段的映射表")
    args = parser.parse_args(argv)
    path = root / "config.json"
    try:
        if args.command == "fields":
            print("字段｜含义｜示例｜用户可能会这么说")
            for field, info in FIELD_INFO.items():
                print("｜".join((field, *info)))
            return 0
        if args.command == "init":
            if path.exists():
                raise ConfigError("config.json 已存在，不覆盖；请用 set 修改")
            cfg = _read_config(root / "config.example.json")
            for field, value in (("resume_path", args.resume), ("city", args.city), ("matcher", args.matcher)):
                cfg = set_field(cfg, field, parse_value(field, value))
        else:
            cfg = _read_config(path) if path.exists() else {}
            if args.command in ("set", "unset") and not path.exists():
                raise ConfigError("config.json 不存在，请先用 init")
            if args.command == "set":
                cfg = set_field(cfg, args.field, parse_value(args.field, args.value))
            elif args.command == "unset":
                cfg = unset_field(cfg, args.field)
        if args.command == "show":
            print(json.dumps(show_config(cfg), ensure_ascii=False, indent=2))
            return 0
        if args.command in ("init", "set", "unset"):
            atomic_write(path, cfg)
            print("✅ config.json 已写入；以下为当前检查结果")
        return print_check(cfg, root, probe_chrome=None if getattr(args, "no_chrome", False) else probe_chrome_port)
    except ConfigError as exc:
        print(f"❌ {exc}")
        return 1
    except (OSError, UnicodeError, ValueError, TypeError):
        # 不输出异常原文，JSON 解析异常可能含个人内容。
        print("❌ 配置不可读、格式无效或写入失败，请检查 config.json / config.example.json")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
