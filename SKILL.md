---
name: job-hunter
description: 为用户引导首次求职配置、用自然语言修改偏好，并在 BOSS、猎聘、应届生、51job、鱼泡筛选岗位与受控投递。支持 Jev、OpenAI 兼容 LLM 和本地关键词匹配。
origin: local
---

# Job Hunter

使用者是用户和用户自己的 agent。若本地有 `docs/mmr.md`（个人交接日志，不提交）先读它恢复当前状态，命令与筛选细节查 [README.md](README.md)。已有简历、偏好和授权沿用，不重复询问。

## 目录规则

1. 不在项目根目录新建运行时文件；新增输出必须经过 `shared.py` 的路径函数：人看的 Markdown 用 `report_path/queue_path` 写到 `reports/`，程序状态用 `log_path/cooldown_path/cache_path/state_path` 写到 `data/` 对应子目录。路径函数不创建目录；每个写函数在写入前创建父目录，原子写临时文件与目标同目录。
2. 新增测试放 `tests/`，文档与交接放 `docs/`；入口与模块保持根目录平铺。
3. 个人文件（`config.json`、`daily.json`、简历、`chrome-debug/`、`data/`、`reports/`）一律不提交。
4. 从旧版平铺布局升级：首次运行前由维护者按 [README 的迁移命令](README.md#从旧布局迁移) 把旧文件移入新目录；不要直接运行后把旧日志遗漏当作没有投递历史。

## 首次使用

`config.json` 不存在或 `python3 config_tool.py check` 不通过时执行。一次性问清以下清单中尚未知的信息；已有配置用 `set` 修复，不重新初始化。

1. 简历文件路径（UTF-8 txt/md，PDF 请用户另存文本）和需要脱敏的姓名等词，映射 `resume_path`、`resume_redact`。
2. 目标城市、身份（届别/在校或社招）、求职方向与岗位关键词，映射 `city`、`target_roles`、`job_profile`。偏好同时写明每周到岗天数、工作城市、实习时长等已知条件，不虚构经历。
3. 根据方向生成四段提问文本，给用户看一眼后写入：`jev_direction`（目标方向）、`jev_title_unrelated`（明显无关岗位）、`jev_stage_question`（符合身份的完整提问）、`jev_fit_unrelated`（不相关的最低档描述）。Jev 与 LLM 共用，保留 `jev_` 前缀。运营方向应移除默认“纯运营不相关”，社招身份不能保留默认在校/2027届实习问题。
4. 匹配方式：有 Jev 密钥可用 `jev`；否则建议 `llm`，推荐便宜的 OpenAI 兼容模型，例如 DeepSeek、通义千问 flash/turbo 档或 GPT mini 档。让用户填写服务商实际 API 根地址和模型 ID；离线时不声称型号、价格或可用性已验证。密钥由用户放入环境变量或 `~/.config/jobhunter/llm.env`，只设置 `llm.api_key_env` 变量名，绝不写密钥到 JSON 或聊天输出。都不想用模型则选 `keyword`，说明只做关键词打分、精度低，建议投递前人工复核。
5. 打招呼语、公司黑名单和每日配额（`daily.json`）可后续补充。`default_count` 是单入口默认份数，不是每日配额。
6. 用以下非交互命令写入，再跑 `check`。提醒用户在端口 9222 的 Chrome 中登录各平台，51job 需完善在线简历。`--no-chrome` 只跳过 TCP 探测，不能证明登录就绪。

```bash
python3 config_tool.py init --resume ./resume.md --city 深圳 --matcher keyword
python3 config_tool.py fields
python3 config_tool.py set resume_redact "张三"
python3 config_tool.py set target_roles "运营，电商运营"
python3 config_tool.py set job_profile "用户确认的身份、求职方向、城市与到岗条件"
# 将用户看过的四段 jev_* 文本分别 set 写入
python3 config_tool.py check
```

`init` 不覆盖现有配置。每次 `set/unset` 原子写入后自动 check，检查失败退出码为 1，修改仍已保存；汇总尚缺项目并修复，不能把它误认为写入失败。提示默认画像是提醒，不单独视为阻塞；方向不同时必须同步更换文本。

## 修改参数

用户说“方向改成运营”“换便宜模型”等自然语言时，agent 查 `python3 config_tool.py fields` 映射字段，直接 `set`，不让用户编辑 JSON。列表可用中文或英文逗号；数值与后端枚举由工具校验。改方向/身份时同步更新 `target_roles/job_profile` 和四段 `jev_*` 文本；换服务商/模型时更新 `matcher/llm.base_url/llm.model/llm.api_key_env` 中对应字段。可选字段用 `unset` 回到默认值，不能删除简历、岗位列表和画像等必填项。

改完把 check 结果及待处理项告诉用户。各投递入口的城市和匹配器默认读配置，显式 CLI 参数优先。BOSS 改简历、偏好或提问后使用 `--refresh`，避免复用旧结论；其他平台会自动隔离材料摘要，LLM 还隔离接口与模型。

## 权限与安全

- Jev/LLM 会外发脱敏简历、偏好、岗位公司和 JD，dry-run 也会请求并计费；先确认用户对该服务商的数据外发授权。密钥存在不代表授权。keyword 不调用模型。
- 在 Chrome 9222 对应用户目录手动登录，不读取/导出认证凭证，不绕过验证码，不重放内部 API。
- 真投前征得对应平台、岗位或配额的同意；已有明确授权不重复确认。首单应针对已核对的具体候选，先完成 dry-run，再 `--calibrate --count 1 --job-id <已确认ID>`（BOSS 按其已有入口）。未校准平台不批量真投。
- 默认 `review-gate`：模型建议复核就进入人工队列，不能因分数高放行；`boss-b` 仅在用户明确选旧策略时使用。
- 提交必须核验当前 ID、标题、公司与按钮，点击前落盘 pending，只原生点击一次。成功要有当前岗位的页面变化或已校准且关联当前职位的接口证据，分别报告，不能把未知 code=0 当成成功。
- pending 不算成功、不重投，出现即停。风控、断连、超时和不明结果不重试，保留记录。用户说停就立即停止，不为填配额继续。

## 平台流程

| 平台 | 入口 | 状态 |
|---|---|---|
| BOSS | `boss_apply.py` | 已实测 |
| 猎聘 | `liepin_apply.py` | 已校准 |
| 应届生求职网 | `yingjiesheng_apply.py` | 已校准 |
| 51job | `51job_apply.py` | 已校准 |
| 鱼泡 | `yupao_apply.py` | 待校准 |

状态与代码门禁有差异时，以具体证据和 `platform_adapters.CALIBRATED` 核对，不靠改文档解除门禁。推荐/复核/跳过只是匹配结论，不等于实投。

先 `--dry-run --matcher keyword` 核对页面、ID 与 JD；获模型外发授权后再用配置后端评估。未知筛选直接报错，不套用其他平台代码。

- 应届生不支持工作类型/经验/学历筛选，用关键词与模型硬条件。已申请、转载、过期跳过；换志愿、补信息、简历不完整、`.delivery-tip` 进入复核。详情状态先于模型/缓存；点击后任何 `.el-dialog` 保留 pending，不操作弹窗。
- 51job JD 只取页面自身搜索请求并按 jobId+标题关联，不重放接口、不开详情页；缺项或空 JD 先复核，校招交应届生，外跳/过期跳过。`--approved` 禁止，用户在页面手动处理。
- 猎聘学历单值，原生选择后核验；鱼泡工作类型/经验/学历参数须为空。鱼泡未知业务端点不能确认网络成功。
- 保持现有慢节奏与详情上限，不自动加 `--fast/--ignore-cooldown`。详情标签只清理工具自建标签，不并发运行投递脚本。

## 复核、日跑与交付

`python3 review_list.py [--city 深圳]` 生成 `reports/复核清单.md`。用户看过并勾选 `[x]` 后，BOSS/猎聘/应届生/鱼泡可用 `--approved --city 深圳 --count 1`；不搜索、不读简历/模型密钥、不调模型，仍核验详情身份、公司、冷却、去重与按钮，未校准平台仍须首单门禁。禁止 `--fill-queue`，BOSS 也禁止 `--fast/--ignore-cooldown`。

日跑 `python3 daily.py [--plan daily.json]` 默认 dry-run；只有对应平台与配额已授权才 `--live`。计划关键词必须精确属于 `target_roles`，不扩词；五平台配额与城市由 daily.json 决定，不能误用 default_count。顺序运行、不轮询；读取退出码、`reports/日报.md` 与 `data/runs/<日期>/` 日志，0 不代表配额全部完成。pending 停该平台，超时/风控不重试。

招呼语补发 `boss_greet.py` 默认预览，单独获发送授权才 `--send`。`inbox_report.py` 只读回复，不点击会话/标已读、不发消息；`apply_tracker.py` 只读本地官网缓存，网申由用户手动完成。官网入口发现仍是独立 Jev+搜索流程，不受 JD matcher 配置影响。

每轮交付本次新增 applied、pending、复核/跳过、停止原因与证据边界，查看 `reports/队列/<平台>-<城市>-队列.md` 和日志。applied 不证明简历附件已送达、HR 回复或面试；mock 测试不能写成实投。个人配置、简历、模型缓存、日志、密钥和认证目录只留本地，禁止提交；新目录及旧根目录 LLM 缓存均已加入 `.gitignore`。
