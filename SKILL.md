---
name: job-hunter
description: 求职自动投递助手。读取用户简历与偏好，在招聘平台批量按匹配度投递。触发词：投简历、自动投递、找工作、job-hunter、帮我投递
origin: local
---

# Job Hunter — 自动求职投递

## 交互流程（Claude 执行步骤）

### Step 0：检查配置

读取 `config.json`（与本 SKILL.md 同级目录）：

- **文件不存在 → 走 Step 1（首次引导）**
- **文件存在 → 跳到 Step 2（确认并投递）**

### Step 1：首次引导（生成 config.json）

向用户说明：「首次使用，我需要了解你的背景来做 JD 匹配。请提供：
1. 简历文件路径（.md / .txt / .pdf 任意）
2. 期望岗位方向（1-3 个，如：产品经理 / 前端工程师）
3. 要排除的岗位关键词（如：总监、架构师、P8 这类不适合你级别的）」

拿到简历路径后：

1. **读取简历文件**（Read 工具）
2. **从简历中抽取 8-15 个技能关键词** —— 包含：
   - 硬技能（编程语言、框架、工具：Python、React、Figma、SQL 等）
   - 业务领域词（电商、B端、支付、AI 等用户实际做过的方向）
   - 跳过：通用软技能（"沟通能力"、"团队协作"），公司名、学校名
3. **生成一个打招呼话术**，基于简历亮点，控制在 80 字内
4. **写入 `config.json`**：

```json
{
  "resume_path": "用户给的路径",
  "greeting": "你基于简历生成的话术",
  "skills": ["抽出的技能1", "技能2", ...],
  "target_roles": ["用户给的方向1", ...],
  "exclude_keywords": ["用户给的排除词1", ...],
  "boost_keywords": ["llm", "大模型", "agent", "rag", "gpt"],
  "company_blacklist": ["外包", "劳务派遣"],
  "min_score": 60,
  "default_count": 20
}
```

5. 展示生成的 config 摘要给用户确认，邀请修改。

### Step 2：收集本次投递信息

一次性问：

> 告诉我本次投递：
> 1. 搜索岗位名（如：产品经理实习）
> 2. 城市（鱼泡支持 50+ 城市；Boss 支持 9 大城市+全国；猎聘支持 12 个常用城市）
> 3. 投递数量（默认 20）
> 4. 平台：Boss直聘 / 鱼泡直聘 / 51job / 猎聘 / 多平台组合

### Step 3：打开浏览器让用户登录

检测 Chrome 调试端口（9222）是否在运行。若未运行，提示用户用以下命令启动：

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/chrome-debug
```

连上后打开对应平台的登录页：

```python
from DrissionPage import ChromiumPage, ChromiumOptions
opts = ChromiumOptions().set_local_port(9222)
page = ChromiumPage(addr_or_opts=opts)
page.new_tab("https://www.zhipin.com/web/user/?ka=header-login")   # Boss
# 或
page.new_tab("https://www.yupao.com/user/login")                    # 鱼泡
```

提示用户：「登录页已打开，完成登录后告诉我」。

### Step 4：运行投递脚本

```bash
cd ~/.claude/skills/job-hunter

# Boss直聘
python3 boss_apply.py --job "产品经理" --city 深圳 --count 20

# 鱼泡直聘（支持 50+ 城市，默认按 config.json 的 min_score 过滤）
python3 yupao_apply.py --job "产品经理实习" --city 深圳 --count 20

# 51job / 前程无忧（需要先在 51job 完善在线简历）
python3 51job_apply.py --job "产品经理" --city 杭州 --count 20

# 猎聘（点岗位标题→新开 JD tab→聊一聊 即视为投递）
python3 liepin_apply.py --job "ai产品经理" --city 深圳 --count 20

# 只想广撒网、不过滤评分：
python3 yupao_apply.py --job "java实习" --city 北京 --count 20 --min-score 0
```

### Step 5：反馈结果

脚本结束后读取 `*-log.json` 给出报告：成功 / 跳过 / 失败数量，日志路径。

---

## 技术栈

- **DrissionPage 4.x** — CDP 连接本地 Chrome（端口 9222）
- **shared.py** — 公共模块：
  - 配置/评分/日志：`load_config` / `load_log` / `save_log` / `score_jd`
  - 通用脚手架：`connect_chrome`（连 9222）/ `parse_common_args`（统一 `--job/--city/--count/--min-score`）/ `record`（追加日志并落盘）/ `print_summary`（结尾汇总框）
  - 去重与黑名单：`load_applied_index`（聚合所有 `*-log.json` 的已投索引）/ `dedup_key`（公司+标题归一化键）/ `is_blacklisted`（公司名子串命中黑名单）

## 文件结构

```
~/.claude/skills/job-hunter/
├── SKILL.md
├── README.md
├── shared.py                 # 公共：配置、评分、日志
├── boss_apply.py             # Boss直聘
├── yupao_apply.py            # 鱼泡直聘
├── config.example.json       # 配置模板
├── config.json               # 用户配置（gitignore，首次引导生成）
├── resume.md                 # 用户简历（gitignore）
└── *-log.json                # 投递日志（gitignore）
```

## 评分逻辑（shared.score_jd）

| 条件 | 加分 |
|------|------|
| 命中 `exclude_keywords` | 归零跳过 |
| 标题含 `target_roles` 之一 | +30 |
| JD 含实习/校招/应届 | +30 |
| JD 命中 `skills` 技能词 | 每个 +5，封顶 +30 |
| JD 命中 `boost_keywords` | +10 |

≥ `min_score` 投递，< `min_score` 跳过。用户改 `config.json` 可调权重。

## 跨运行去重 & 公司黑名单

四个平台统一接入，投递前先拦截：

- **跨运行去重**：启动时 `load_applied_index()` 扫描本目录所有 `*-log.json` 的 `applied[]`，聚合成"已投索引"。去重键 = `归一化(公司名) + 归一化(岗位标题)`，**跨搜索、跨平台生效**。已投过的同一岗位在任何平台都不再重复投（记为 `skipped`，reason=`已投过（跨运行去重）`）。删掉某个 log 文件即自动"遗忘"。
  - 公司名缺失的平台（鱼泡左列表、部分 51job 卡片）退化为**按标题去重**；boss/猎聘/51job 能拿到公司名时是"公司+标题"精确去重。
- **公司黑名单**：`config.json` 的 `company_blacklist`（子串、大小写不敏感）命中公司名 → 永远跳过（reason=`公司黑名单`）。鱼泡拿不到公司名，黑名单对其不生效。

## 平台差异速查

| 平台 | 投递方式 | 分页 | 城市参数 | 前置要求 |
|------|---------|------|---------|---------|
| Boss直聘 | 右侧"立即沟通" | 无限滚动 | 9 大城市+全国 | 登录 |
| 鱼泡直聘 | "发送简历"优先，降级"聊一聊" | 滚动加载 | 50+ 城市 | 登录 |
| 51job | 搜索页内联 `button.btn.apply` | `&pageNum=N` | 北京/上海/广州/深圳/杭州 | 登录 + 完善 51job 在线简历 |
| 猎聘 | 点标题→新开 JD tab→"聊一聊"；"继续聊"=已投跳过 | `&currentPage=N` (0 起) | 12 个常用城市 | 登录 |

**51job 特别说明**：
- 部分校招岗位链接跳到 `yingjiesheng.com`（应届生求职网），脚本会**自动跳过**这类卡片
- 首屏卡片点击有时静默失败（服务端节流/Vue 懒加载），脚本已加重试 + 预热滚动，但单轮可能要多遍历几张卡片才能投满目标
