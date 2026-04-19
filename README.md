# Job Hunter — AI 辅助的求职自动投递

> 与其广撒网，不如精准慢投。让 AI 读你的简历，只投匹配的岗位。

这是一个 [Claude Code](https://claude.com/claude-code) 的 **skill**，把你的简历读一遍、记住你的方向和底线，然后在招聘平台上按岗位匹配度自动点击投递按钮。

## 它能做什么

- 📄 **读你的简历**，提取技能关键词（8-15 个），存到本地配置
- 🎯 **按 JD 匹配度投递**：命中你技能 / 目标岗位词的才投，命中排除词直接跳过
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

# Boss 直聘
python3 boss_apply.py --job "产品经理" --city 深圳 --count 20

# 关掉评分过滤，全投
python3 yupao_apply.py --job "Java实习" --city 北京 --count 20 --min-score 0
```

## 配置说明

首次引导后会生成 `config.json`，字段含义：

| 字段 | 作用 |
|------|------|
| `resume_path` | 简历文件路径 |
| `greeting` | 打招呼话术（目前仅存储，脚本默认走平台沟通流程） |
| `skills` | 你的技能词 → JD 每命中一个 +5 分，封顶 +30 |
| `target_roles` | 期望岗位 → 标题命中 +30 分 |
| `exclude_keywords` | 排除词 → 命中直接归零（如 "总监"、"架构师"） |
| `boost_keywords` | 加权词 → JD 命中 +10 分（AI/LLM 等） |
| `min_score` | 最低投递分，默认 60 |
| `default_count` | 默认投递数量 |

随时手改 `config.json` 即可调整权重。

## Token 消耗预估

| 阶段 | Tokens |
|------|--------|
| 首次引导（读简历 + 抽技能 + 生成话术） | ~6K |
| 每次启动投递（读 config + 运行脚本 + 汇报结果） | ~3K |
| 本地评分匹配 | **0**（纯 Python 规则，不走 LLM） |

投 100 份岗位的总成本 ≈ 读简历一次（一次性） + 3K 启动。**不按岗位数量线性增长**。

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

- 脚本只在你**本地浏览器**跑，不上传简历到任何第三方
- 投递动作是**真实点击**，和你手工操作等价 —— 平台接受即接受
- 遵守平台的日沟通上限，不会主动绕过
- 日志在本地 `*-log.json`（`.gitignore`），不外传

## 开源许可

MIT

## 致谢

用 [DrissionPage](https://github.com/g1879/DrissionPage) 做浏览器控制，比 Selenium 轻很多。
