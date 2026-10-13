# Job Hunter — AI 辅助的求职自动投递

> 与其广撒网，不如精准慢投。让 AI 读你的简历，只投匹配的岗位。

这是一个 [Claude Code](https://claude.com/claude-code) 的 **skill**，把你的简历读一遍、记住你的方向和底线，然后在招聘平台上按岗位匹配度自动点击投递按钮。

## 它能做什么

- 📄 **读你的简历**，提取技能关键词（8-15 个），存到本地配置
- 🎯 **按 JD 匹配度投递**：Boss 默认用 Jev 匹配 JD × 脱敏简历，分为推荐 / 复核 / 跳过；其他平台继续关键词评分
- 🌐 **多平台**：Boss 直聘、鱼泡直聘、前程无忧 51job
- 🗂️ **完整日志**：每次投递记录岗位、评分、动作，避免重复投

## 它不做什么

- ❌ **不伪造简历**、不代写求职信
- ❌ **不绕过平台反爬** — 用本地 Chrome 真实浏览器跑，会被 IP 风控/频率限制
- ❌ **不帮你面试** — 投递是第一步，HR 还是得自己聊

## 安装

### 1. 放到 Claude Code skills 目录

```bash
git clone https://github.com/<your>/job-hunter.git ~/.claude/skills/job-hunter
```

### 2. 安装依赖

```bash
pip install DrissionPage
```

### 3. 启动远程调试的 Chrome

```bash
# macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/chrome-debug

# Windows
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --user-data-dir=C:\temp\chrome-debug

# Linux
google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug
```

在这个 Chrome 里登录 Boss / 鱼泡。

### 4. 准备简历

把你的简历丢到任意位置（支持 `.md` / `.txt`；PDF 请先转文本）：

```bash
cp ~/Documents/my-resume.md ~/.claude/skills/job-hunter/resume.md
```

## 使用

在 Claude Code 里说一句触发词就行：

```
帮我投简历
自动投递
job-hunter
```

**首次运行**，Claude 会：
1. 问你简历路径、期望岗位方向、要排除的岗位词
2. 读简历、抽技能、生成打招呼话术
3. 写入 `config.json`，展示给你确认

**后续运行**，直接问 "岗位 / 城市 / 数量 / 平台"，然后开始投。

## 手动运行（不走 Claude）

```bash
cd ~/.claude/skills/job-hunter

# 鱼泡（支持 50+ 城市）
python3 yupao_apply.py --job "产品经理实习" --city 深圳 --count 20

# Boss 直聘（默认 Jev，先配置 key、文本简历和 job_profile）
python3 boss_apply.py --job "产品经理" --city 深圳 --count 20

# Boss 只浏览 JD、打印职位 ID 和评分，不点“立即沟通”（count 为评分上限）
python3 boss_apply.py --job "产品经理" --city 深圳 --count 20 --dry-run

# Boss 使用原关键词评分（不调用 Jev，仍按 min_score 过滤）
python3 boss_apply.py --job "产品经理" --city 深圳 --matcher keyword --min-score 60

# 关掉评分过滤，全投
python3 yupao_apply.py --job "Java实习" --city 北京 --count 20 --min-score 0
```

**Boss 核验与节奏**：卡片按职位 ID 定位；缺 ID 记 `skipped`（无职位ID）。仅接口 HTTP 200 且 `code=0` 记 `applied`（`status=chat_started`）并计入成功；未捕获响应记 `pending`，不计数、不重试，并加入去重。明确错误记 `failed`；接口风控或页面验证记 `failed`（`status=blocked`），汇总后立即退出。

Boss 日志的 `evidence.packets` 保存本次点击后 8 秒核验窗口内捕获的全部 `friend/add` 命中包：`url`、`http_status`、原始 `code` 和 `message`（最多 200 字）。监听路径和验证弹层选择器仍需实测校准；多包时风控优先终止，其余以捕获到接口确认为成功。去重先查 `boss:{job_id}`，再查公司+岗位旧键，兼容旧日志及跨平台去重。

默认每次沟通点击后随机等待 30–90 秒，每确认成功 5 份额外休息 3–5 分钟，浏览等待有 ±25% 抖动。`--fast` **仅供调试**，将沟通间隔及额外休息降为 3–6 秒，核验窗口仍为 8 秒。`--dry-run` 不沟通、不写 `applied/pending`；Jev 模式仍请求或读取缓存，写 `review/skipped` 和队列，keyword 模式保留只打印评分、不写评分 `skipped` 的旧行为。缺 ID、黑名单、历史去重仍可记录跳过。两个开关目前只有 Boss 实际使用。

## Boss 的 Jev 匹配

设置环境变量 `JEV_API_KEY`，或在仓库外的 `~/.config/jobhunter/jev.env` 保存一行 `JEV_API_KEY=<你的 key>`（建议文件权限 `600`）。环境变量优先；默认 `--matcher jev` 缺 key 会醒目报错并退出，不静默降级。不要把真实 key 写进配置、命令示例、日志或仓库。

`config.json` 设置 UTF-8 `.md/.txt` 的 `resume_path`、`job_profile` 和 `resume_redact`；相对简历路径按脚本目录解析，PDF 请先转文本。每次将标题、公司、完整 JD、脱敏简历及求职偏好合成 state，一次请求三个问题：硬条件 `hard_ok`、核心职责匹配 `fit`、处理建议 `action`。接口结构见 [TypeSafe API](https://docs.typesafe.ai/api) 与 [问题类型](https://docs.typesafe.ai/primitives)。

硬条件拆成三问（学历 / 在校·应届 / 到岗·时长·城市），`hard_ok` 取最小值，原因会写明卡在哪一项。规则依次执行：`hard_ok < 0.2` 跳过；Jev 建议跳过且该选项概率 ≥ 0.6 时跳过；`fit_ev < 1.5` 跳过；`hard_ok ≥ 0.6`、`fit_ev ≥ 2.3` 且 Jev 未建议跳过即推荐投递；其余人工复核。JD 送 Jev 前会去掉 Boss 反爬水印字与页脚。`fit_ev` 用四档概率的期望值计算，范围 0–3。noul 用于挡明确冲突，较高数值也不是“满足硬条件”的事实证明；这些阈值仍需用真实岗位人工校准。

Jev 模式的 `exclude_keywords` **只匹配标题**，黑名单和历史去重在请求前拦截。`review` 不沟通、不计成功，也不进入去重索引；调用或响应解析失败进入复核，连续失败 3 次停止。只有 `apply` 进入原沟通核验流程。`--min-score` 仅影响 keyword 模式，鱼泡 / 51job / 猎聘仍使用 `score_jd`。

缓存 `jev-cache.json` 与日志同目录，键为 `boss:{job_id}:{JD 的 SHA1 前12位}`，保存原始 answers、决策和时间，不保存简历/JD/key。相同职位与 JD 命中后不再请求；修改简历、求职偏好或问题文本后，应删除缓存再评。阈值修改会用缓存 answers 重新决策。

每次结束（包括 dry-run、风控或连续调用失败退出）覆盖 `boss-{城市}-队列.md`：推荐 / 复核 / 跳过 / 已沟通 / 待核验统计，人工复核表、推荐但未投表、最多 50 行跳过表；dry-run 额外列出旧 `score_jd` 分数对照。读取 JD 前拦截的岗位显示 `—`，避免把未读取的 JD 当成完整评分。队列仅包含本次已处理岗位。

正式 Jev 模式达到 `--count` 后评完当前已加载批次，余下推荐留在队列，不再沟通或加载新批次；dry-run 的 `--count` 仍是评估数量上限。keyword 保持达到数量后立即停止的旧行为。

## 配置说明

首次引导后会生成 `config.json`，字段含义：

| 字段 | 作用 |
|------|------|
| `resume_path` | 简历文件路径 |
| `job_profile` | Jev 求职偏好：学历/届别/方向/城市/到岗与实习时长等，默认空字符串 |
| `resume_redact` | 发送前替换为 `[已隐去]` 的字符串列表，如姓名；默认 `[]`，手机号与邮箱自动脱敏 |
| `greeting` | 打招呼话术（目前仅存储，脚本默认走平台沟通流程） |
| `skills` | 你的技能词 → JD 每命中一个 +5 分，封顶 +30 |
| `target_roles` | 期望岗位 → 标题命中 +30 分 |
| `exclude_keywords` | Boss Jev 只匹配标题；keyword 及其他平台匹配标题 + JD，命中归零 |
| `boost_keywords` | 加权词 → JD 命中 +10 分（AI/LLM 等） |
| `min_score` | keyword 最低投递分，默认 60；Boss Jev 不使用 |
| `default_count` | 默认投递数量 |

随时手改 `config.json` 即可调整权重。

## Token 消耗预估

| 阶段 | Tokens |
|------|--------|
| 首次引导（读简历 + 抽技能 + 生成话术） | ~6K |
| 每次启动投递（读 config + 运行脚本 + 汇报结果） | ~3K |
| keyword 本地评分匹配 | **0**（纯 Python 规则，不走 LLM） |
| Boss Jev 匹配 | 每个未命中缓存的岗位一次 API 请求，按 Jev 实际用量计费 |

上面的启动 Token 数是粗略估计；Jev 模式还会发送脱敏简历和 JD，API 用量随未缓存岗位数量及材料长度增加，dry-run 也会产生请求用量。

## 平台支持

| 平台 | 投递方式 | 城市 | 前置要求 |
|------|---------|------|---------|
| Boss 直聘 | "立即沟通" | 9 大城市 + 全国 | 登录 |
| 鱼泡直聘 | "发送简历"优先，降级"聊一聊" | 50+ 城市 | 登录 |
| 前程无忧 51job | 搜索页内联"投递"按钮 | 北京/上海/广州/深圳/杭州 | **登录 + 完善 51job 在线简历**（必须有在线简历，否则投递会被踢回简历向导页） |

**51job 特别说明**：
- 跳转到 `yingjiesheng.com`（应届生求职网）的校招岗位会被**自动跳过**
- 首屏卡片因 Vue 懒加载有时首次点击失败，已加重试，但仍可能需要多遍历几张卡片

## 安全与伦理

- 浏览器控制在本地运行；**启用 Jev 后，脱敏简历 + JD + 岗位/公司 + 求职偏好会发送到 `api.typesafe.ai`**，并非全程不上传第三方
- 手机号（大陆 11 位）、邮箱及 `resume_redact` 指定项会替换为 `[已隐去]`；其他学校/项目等内容仍可能识别个人，发送前检查材料及自定义隐去项。keyword 模式不调用 Jev
- 投递动作是**真实点击**，和你手工操作等价 —— 平台接受即接受
- 遵守平台的日沟通上限，不会主动绕过
- 日志、`jev-cache.json` 和 `*-队列.md` 均保存在本地并加入 `.gitignore`；不在其中保存 Jev key 或发送的简历原文

## 开源许可

MIT

## 致谢

用 [DrissionPage](https://github.com/g1879/DrissionPage) 做浏览器控制，比 Selenium 轻很多。
