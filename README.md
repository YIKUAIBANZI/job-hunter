# Job Hunter — AI 辅助求职

用本地 Chrome 读取招聘岗位，按简历和偏好筛选，再通过浏览器点击提交并核验。支持 BOSS、猎聘、应届生求职网、51job、鱼泡；不会重放平台内部接口，不处理或绕过验证码。

## 目录结构

入口与模块保持平铺，命令仍从项目根目录执行，例如 `python3 boss_apply.py ...`。路径统一由 `shared.py` 计算，写函数负责创建父目录。

```text
jobhunter/
├── README.md  SKILL.md  .gitignore
├── *.py                         # 平铺入口与模块，命令不变
├── config.example.json  daily.example.json  # 可提交的示例
├── config.json  daily.json  resume.*        # 个人文件，忽略，位置不变
├── reports/                     # 给人看的输出，忽略
│   ├── 复核清单.md  日报.md  回复日报.md  网申进度.md  官网投递.md
│   └── 队列/<平台>-<城市>-队列.md
├── data/                        # 程序状态，忽略
│   ├── logs/<平台>-<城市>-log.json
│   ├── cache/                   # jev-cache.json、llm-cache.json、
│   │                           # jev-<平台>-cache.json、llm-<平台>-cache.json、official-cache.json
│   ├── cooldown/<平台>-cooldown.json
│   ├── boss-greet.json
│   ├── runs/<日期>/...          # daily 子进程输出
│   └── evidence/               # 校准证据
├── docs/                        # platform-upgrade-spec.md 提交；mmr.md 为本地个人交接日志，不提交
├── tests/                       # test_*.py、conftest.py，提交
└── chrome-debug/                # 浏览器用户目录，忽略，位置不变
```

- `reports/`：报告与人工勾选清单；`reports/队列/` 保存各平台本轮队列。
- `data/`：程序状态；`logs/` 保存投递与防重日志，`cache/` 保存模型及官网缓存，`cooldown/` 保存冷却截止时间。
- `data/runs/`：按日期保存日跑子进程输出。
- `data/evidence/`：保存校准 JSON 和平台验证报告，个人证据只留本地。
- `docs/`：开发规格（提交）；`docs/mmr.md` 是本地个人交接日志（含投递记录，已忽略不提交），升级规格见 [docs/platform-upgrade-spec.md](docs/platform-upgrade-spec.md)。
- `tests/`：离线回归测试，支持 `python3 -m pytest -q` 与 `python3 -m pytest -q tests`。
- `chrome-debug/`：保留浏览器用户目录；个人配置、日跑计划和简历仍相对项目根解析。

## 从旧布局迁移

由维护者在项目根目录、首次运行新版前执行。代码不会自动迁移或读取旧根目录日志；先迁移才能保留去重、冷却、批准勾选与缓存。此轮实现未移动任何已有个人数据。

下面显式使用 Bash，避免 zsh 在 glob 无匹配时报错。不存在的文件跳过；`mv -n` 不覆盖同名目标，若两处已有同名文件或 `data/runs/` 已存在，先人工合并再运行。

```bash
bash <<'SH'
mkdir -p data/logs data/cache data/cooldown data/evidence reports/队列
for f in *-log.json; do
  if [ -f "$f" ]; then mv -n "$f" data/logs/; fi
done
for f in *-cooldown.json; do
  if [ -f "$f" ]; then mv -n "$f" data/cooldown/; fi
done
for f in jev-cache.json* llm-cache.json* jev-*-cache.json* llm-*-cache.json* official-cache.json; do
  if [ -f "$f" ]; then mv -n "$f" data/cache/; fi
done
if [ -f boss-greet.json ]; then mv -n boss-greet.json data/; fi
if [ -d runs ] && [ ! -e data/runs ]; then mv runs data/runs; fi
for f in evidence/*.json evidence/platform-validation-*.md; do
  if [ -f "$f" ]; then mv -n "$f" data/evidence/; fi
done
for f in 复核清单.md 日报.md 回复日报.md 网申进度.md 官网投递.md; do
  if [ -f "$f" ]; then mv -n "$f" reports/; fi
done
for f in *-队列.md; do
  if [ -f "$f" ]; then mv -n "$f" reports/队列/; fi
done
SH
```

`config.json`、`daily.json`、`resume.*`、`chrome-debug/` 保持原位；`evidence/` 的其他文件保留原位。测试和两份开发文档已由本次代码整理移动，无需混入个人数据迁移命令。

## 当前能力与边界

BOSS 保留已实测流程，其余平台共用运行器。**离线测试或只读浏览不代表投递成功**，逐平台证据见 [验证记录](data/evidence/platform-validation-2026-10-14.md)。

五个投递入口支持 `jev/llm/keyword`，默认读取 `config.matcher`（缺省 jev）；`--city` 默认读取 `config.city`（缺省深圳），显式 CLI 参数优先。`--dry-run` 阻止投递和聊天动作，但模型模式仍会请求接口。keyword 可能把销售、研发岗位判高分，也可能因正文出现“资深”误杀实习岗位。

| 平台 | 入口 | 校准状态 |
|---|---|---|
| BOSS | `boss_apply.py` | 保留已实测流程 |
| 猎聘 | `liepin_apply.py` | 已校准 |
| 应届生求职网 `yingjiesheng` | `yingjiesheng_apply.py` | 已校准 |
| 51job | `51job_apply.py` | 已校准 |
| 鱼泡 | `yupao_apply.py` | 已校准（对口岗位少，默认配额 0） |

应届生搜索与详情数据只监听页面自身请求，详情在自建标签直接导航并于结束关闭；投递须 ID + 标题一致、状态允许、原生点击“立即申请”。卡片已申请、转载或过期直接跳过；志愿替换、企业补充信息及简历不完整进入人工复核，均不调模型。详情状态检查先于标题粗筛与匹配缓存。可见 `.delivery-tip` 记录其文字并复核；点击后出现 `.el-dialog` 保留 pending 并停止，不点击任何弹窗按钮。

应届生暂不支持 `--job-type/--experience/--degree`，非空参数直接报错，请用关键词 + Jev 硬条件。保持详情间隔 15–30 秒，每 5 个休息 90–180 秒，默认 `--max-views 20`。日志、冷却与缓存分别为 `data/logs/yingjiesheng-<城市>-log.json`、`data/cooldown/yingjiesheng-cooldown.json`、`data/cache/jev-yingjiesheng-cache.json`，均由现有忽略规则覆盖。

51job 的完整 JD 取自搜索页自身请求的 `we.51job.com/api/job/search-pc`，只监听页面请求，不重放接口、不打开详情页。导航前开始监听，原生筛选后取最后一包 `resultbody.job.items`；按卡片 sensorsdata 的 jobId + jobTitle 关联，缺项、标题不一致或 JD 为空进入复核，不调 Jev。已申请岗位跳过；校招岗位（`isFromXyx=True`）交给 `yingjiesheng_apply.py`，转载/外跳/过期岗位跳过。投递按钮仍从列表卡片核验，首单校准门禁不变。本阶段仅离线验证。

共同流程包含：

- 平台职位 ID 定位、卡片与详情 ID/标题核对；未知身份进入复核。
- Jev 标题粗筛、完整 JD 匹配、7 天结论缓存、复核队列。
- `<平台>:<id>` 与公司+标题双去重，`pending` 也阻止重复提交。公司未知时只按 ID 去重，正式提交前必须取得公司信息。
- 点击前原子写入 `pending`，监听 XHR/Fetch 后只原生点击一次。断连、超时、未知结果不重试，停止本轮。
- 仅已校准端点且请求关联当前职位的响应可确认网络成功。未知接口 `code=0` 无效；当前岗位按钮在点击后变为明确成功状态也可确认。两路证据分别保存，不把其中一路伪称双证据。
- 页面/接口出现风控立即停，写入该平台 2 小时冷却。登录或简历向导需要人工处理。
- 猎聘/鱼泡详情间隔 6–15 秒，每 10 份额外休息 45–90 秒，默认最多 60 份详情。51job 列表 JD 评估间隔 6–12 秒，每 8 份额外休息 60–120 秒，默认最多 30 份；显式 `--max-views` 优先（1–60）。各平台投递后 30–90 秒，每 5 次成功额外休息 3–5 分钟，默认最多 5 页搜索，投满停止。节奏不保证免受风控，搜索页风控仍会停止运行。
- 搜索标签复用；详情标签按 `openerId` 确认归属并清理。避免同时运行多个投递脚本或手动操作正在自动浏览的同一搜索页。

## 快速开始

需要 Python 3.10+、DrissionPage 4.x 和 Chrome。按“装依赖 → 启动 Chrome 并登录 → agent 引导配置 → dry-run → 授权真投”执行：

```bash
python3 -m pip install DrissionPage pytest

# macOS；此目录保留平台登录状态，不要上传
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/chrome-debug"
```

在该 Chrome 中手动登录目标平台。51job 还需填写在线简历。

让自己的 agent 按 [SKILL.md](SKILL.md) 的首次使用清单，一次性收集简历路径、脱敏姓名、身份/届别、方向、城市与匹配方式，再用 `config_tool.py` 配置；也可手动调用：

```bash
python3 config_tool.py init --resume ./resume.md --city 深圳 --matcher keyword
python3 config_tool.py fields
python3 config_tool.py set target_roles "AI产品，产品实习"
python3 config_tool.py set job_profile "你的真实身份、方向、城市与到岗条件"
python3 config_tool.py set resume_redact "需要隐去的姓名"
python3 config_tool.py check
# 离线只检查文件/配置，跳过本机 TCP 探测：check --no-chrome
```

`init` 从示例生成配置，已存在时拒绝覆盖。`set` 支持 `llm.model` 等点路径；列表用中文/英文逗号，数值和枚举自动校验，未知字段报错。`unset` 删除可选字段回到默认。每次写入后自动 check；有 ❌ 返回 1，已保存的修改仍保留。`show` 展示合并默认值后的配置和密钥“已配置/未配置”状态，不打印密钥。check 只探测 Chrome 9222 的 TCP 连通性，不保证平台已登录。

之后只需对 agent 说“方向改成运营”“换便宜模型”；agent 查 fields、修改对应字段并报告 check 结果。改变方向或身份时，要同步更新下面四段提问，不能保留不适用的默认画像。

配置就绪后先试跑，审阅队列；获用户对具体平台/岗位或配额的真实投递授权后，才去掉 `--dry-run`，未校准平台先做首单校准：

```bash
python3 boss_apply.py --job "AI产品" --count 2 --dry-run
# 仅在已获对应授权、核对候选和平台状态后执行
python3 boss_apply.py --job "AI产品" --count 1
```

不要提交个人配置、简历、日志、浏览器目录、密钥或模型缓存。`.gitignore` 已覆盖 `data/`、`reports/` 和旧根目录模型缓存，兼容尚未迁移的个人文件。

Jev 的标题粗筛、身份阶段、能力匹配提问默认按“2027 届求 AI 产品实习”编写。求职方向不同时，在 `config.json` 中覆盖（均为可选字符串）：

| 字段 | 默认 | 用途 |
|---|---|---|
| `jev_direction` | 产品/AI产品方向实习 | 标题粗筛：是否可能是候选人想要的方向 |
| `jev_title_unrelated` | 销售、剪辑、设计、编剧、标注、教练、客服、纯技术研发 | 标题粗筛：明显无关的例子 |
| `jev_stage_question` | 岗位是否接受在校生实习或2027届应届生？… | 硬条件“在校/应届”的完整提问 |
| `jev_fit_unrelated` | 不相关或非产品岗（如销售、剪辑、纯运营） | 能力匹配最低档描述 |

## 匹配方式

| 后端 | 精度/判断方式 | 成本 | 是否外发数据 | 需要什么 |
|---|---|---|---|---|
| `jev`（默认） | TypeSafe Jev 概率化的 noul/choice/score，沿用现有阈值 | 按服务计费 | 是，脱敏简历、偏好、JD 与岗位公司 | JEV_API_KEY |
| `llm` | OpenAI 兼容模型输出同结构 JSON；效果需按岗位校验，自报概率未经统计校准 | 按所选模型计费，建议便宜档 | 是，同一组脱敏资料发送给自选服务商 | base_url、model、外置密钥 |
| `keyword` | 只看配置关键词打分，不读简历做模型推理，精度低 | 无模型费用 | 无模型外发；仍需浏览招聘平台 | target_roles、skills 等关键词 |

没有 Jev 时建议选择便宜的 OpenAI 兼容模型，如 DeepSeek、通义千问 flash/turbo 档、GPT mini 档。具体模型 ID、接口地址、最新价格和是否支持概率化匹配，需按服务商文档和样本实测确认；这些类别不是精度或价格承诺。都不想用模型可选 keyword，建议投递前人工复核。

实测（2026-10-14，LongCat-2.0，51job 深圳 15 个岗位）：便宜模型的概率多为 0/1 两极，比 Jev 更容易判“投”也更容易判“跳过”（Jev 复核的 2 个判投、1 个判跳过），偶有漏题按解析失败进复核。建议 llm 模式先 dry-run 看一轮队列，再开真投。

LLM 配置示例（替换为自己的服务商和实际模型 ID；base_url 是含版本路径的 API 根地址，不含 `/chat/completions`）：

```json
{
  "matcher": "llm",
  "llm": {
    "base_url": "https://api.example.com/v1",
    "model": "服务商实际的便宜模型ID",
    "api_key_env": "JOBHUNTER_LLM_KEY"
  }
}
```

```bash
python3 config_tool.py set llm.base_url https://api.example.com/v1
python3 config_tool.py set llm.model 服务商实际的模型ID
python3 config_tool.py set matcher llm
```

密钥读取顺序：`llm.api_key_env` 指定的环境变量 → `~/.config/jobhunter/llm.env` 中同名的 `JOBHUNTER_LLM_KEY=...` 行（可用单双引号包裹值）。密钥绝不写 config.json；变量名可用 `set llm.api_key_env 自定义变量名` 更换。缺 base_url/model/密钥时退出，提示 `python3 config_tool.py check`，不降级、不连接浏览器。

LLM 使用标准库 urllib POST `{base_url}/chat/completions`，temperature=0，优先 JSON response_format；HTTP 400 时仅去掉该字段重试一次。完整题目原文复用四个 `jev_*` 配置。合法非零概率分布会归一化；缺题、非法答案或请求失败进入人工复核，连续三次失败停止，阈值和 review-gate 与 Jev 共用。LLM 的分数或概率不是满足资格的证明。

Jev 缓存文件名和键保持原样，位置统一为 `data/cache/`；LLM 使用 `data/cache/llm-cache.json`（BOSS）与 `data/cache/llm-<平台>-cache.json`，接口/模型也参与缓存键。两种模型缓存都只保存结构化判断与时间，不保存简历、JD 或密钥。官网入口发现 `official_apply.py` 是独立搜索+Jev 流程，不使用这里的 JD matcher 设置。

## 一键日跑与日报

`daily.py` 只通过子进程调用现有入口，不增加投递流程或依赖。一次调用等到全部平台及后处理结束，覆盖生成 `reports/日报.md`，最后在 stdout 打印一行摘要；上层 agent 读取退出码和日报即可，无需轮询。

```bash
cp daily.example.json daily.json
# 先检查计划里的所有 keywords 都精确属于 config.json target_roles
python3 daily.py                       # 默认试跑，所有投递入口加 --dry-run
python3 daily.py --plan daily.json     # 也可指定其他计划路径
python3 daily.py --live                # 仅在已授权的平台与配额范围内真投
```

计划必须包含 `city`、五个平台的 `quota/keywords/args`，以及 `greet/inbox/review_list` 三个布尔值。关键词按完整字符串校验，不做包含匹配、不自动扩词，零配额平台也校验；越界或缺字段在启动任何子进程前报错。当前示例使用 `AI产品`、`产品实习`，应届生配额为 0、关键词为 `产品经理`；必须先将计划关键词与 `config.target_roles` 白名单对齐。旧四平台计划需要补齐 `yingjiesheng`。`AI产品经理` 与 `AI产品实习` 不因包含 `AI产品` 而被允许。

按 BOSS → 猎聘 → 应届生 → 51job → 鱼泡顺序运行；`quota=0` 跳过并在日报注明。每次搜索的 `--count` 是平台剩余额度：真投按分类内新增的唯一 applied ID 扣减，试跑按入口输出的评估数量扣减（无统计时按传入额度扣减）。额度用完停止；新增 pending 也停止该平台剩余关键词，留待人工核验。试跑仍会读取真实浏览器，默认 Jev 仍会外发脱敏材料并计费；`args` 可用 `--matcher keyword` 做本地匹配。

`args` 只接受筛选、匹配和扫描上限参数：`--job-type/--experience/--degree/--matcher/--min-score/--max-views/--max-pages/--match-policy review-gate/--refresh`（平台是否支持仍由其入口判断）。禁止覆盖关键词、城市、配额、运行模式；`--fast`、`--ignore-cooldown`、`--match-policy boss-b`、`--calibrate` 等在计划校验时直接拒绝。

每个入口最多运行 90 分钟，超时终止且不重试，读取已落盘日志后继续下一平台。stdout/stderr 合流写入 `data/runs/<日期>/<平台>-<序号>.log`，后处理写入同目录的脚本名日志；同日再次运行会覆盖同名日志及日报，需保留证据时先另存。实际运行的平台之间随机休息 60–180 秒，试跑也休息；`--no-rest` 仅供测试/调试。

退出码 2 或入口明确输出“冷却中”记冷却；退出码 1 或新增 blocked 记风控；其他非零记异常，均停止该平台剩余关键词并继续下一平台。新平台目前有些停止路径返回 0，因此也识别其明确停止文案。真投时输出“尚未首单校准”记“未校准，跳过”（兼容门禁返回非零）；新增 blocked 仍优先记风控。日跑不校准、不重试，也不会仅凭普通异常猜测平台是否已校准。

平台结束后按顺序执行：仅 `greet=true` 且真投时补发 BOSS 招呼语（`--send --count 10`）；`review_list=true` 时生成复核清单；`inbox=true` 时读取回复日报。默认启用后两项，招呼语默认关闭。后处理也有日志和超时；失败时在日报注明，已有回复日报的统计标记为“非本次更新”。

日报包含各平台本次新增已投、待核验、复核、跳过、状态及日志链接，另列新增已投岗位、复核清单新增 ID 数和回复日报首部的 HR 已回复数。所有计数按平台、分类和岗位 ID 去重；历史 applied 不计，pending 转 applied 只算新增已投，已确认成功的预记不算待核验。平台整体退出码：任一平台风控为 1，其余运行状态为 0；计划/配置启动校验错误为 2。退出码 0 可能包含冷却、未校准或异常，须结合日报查看。

`daily.json`、`data/runs/`、`reports/日报.md` 均已忽略提交。日跑本阶段仅经 mock 子进程与临时日志离线验证，未连接浏览器、未真投。

## 官网/邮箱清单

把 boss、猎聘、应届生、51job、鱼泡日志中 applied/review 的对口公司合并，方便再手动进行官网或邮件投递；skipped/failed/pending 不入选。评估读取 JD 时提取最多 5 个投递邮箱，日志只存邮箱字符串，不存 JD 原文。

```bash
python3 official_apply.py                 # 有 Tavily、Jev 密钥时搜索并核验官网
python3 official_apply.py --emails-only   # 离线：日志邮箱 + 30 天内已缓存官网
python3 apply_tracker.py                 # 离线更新网申进度，保留手填字段
```

清单输出到 `reports/官网投递.md`，进度跟踪在 `reports/网申进度.md`。缺少 Tavily 密钥时自动降级为 `--emails-only`，不调用 Tavily/Jev、不改官网缓存；`--refresh` 在离线模式不触发查询。官网“已核验”表示模型判断及链接可达，投递前仍需人工确认身份和岗位开放情况。邮件标题建议：姓名-学校-岗位-到岗时间（请按 JD 要求）。

应届生“需补充企业要求的信息”岗位仍进行标题粗筛与 JD 匹配，跳过结论记 skipped，对口或待复核结论记带 `[需表单]` 前缀的 review（`manual_form: true`）；dry-run 和真投均不会提交。清单中的“平台表单待手动投递（应届生）”与网申进度会列出这些岗位，需本人前往平台填写企业表单，复核清单不可批准自动投递。该清单按岗位 ID 取最新、排除已投，离线模式同样可用。

## 秋招网申进度（离线）

```bash
python3 apply_tracker.py
python3 apply_tracker.py --add 公司名 https://careers.example.com/campus
```

只读取本地 `data/cache/official-cache.json` 和 `data/logs/*-log.json`，生成已忽略提交的 `reports/网申进度.md`；不联网、不打开浏览器、不自动填写任何网申表单。每家公司最多两个唯一官网入口，按“已核验”优先、official 概率降序选取；另外写入 JD 邮箱的 `mailto:` 入口（核验列“JD邮箱”）和需补表单的岗位入口（“平台表单”）。标题最多显示 30 字，来源岗位用“、”连接。缓存不存在时仍可维护邮箱、表单及手动入口；缓存损坏则报错并保留旧进度表。

在主表手填截止日期（`YYYY-MM-DD`）、状态与备注，再次生成按去查询参数、片段与末尾斜杠的 URL 回填。状态可用“未投/已投/笔试/测评/面试/Offer/结束”（其中“笔试/测评”为一个状态）；其他文本保留，计入“其他”。缓存中消失的行仅在状态不为“未投”或有备注时保留，核验列加“(缓存已过期)”；只填截止日期的消失行不保留。`--add` 可重复使用，手动入口持续保留。

顶部统计所有入口；7 天内提醒包含今天与第 7 天，按日期升序，不含已过期、无效日期与“结束”行。计划可选 `"tracker": true`：在复核清单/回复日报之后调用该离线 CLI，日报“需要你处理”读取进度表首部的入口数与即将截止数。省略时默认关闭，`daily.example.json` 已启用；生成失败时注明统计非本次更新或不可读。

## 先只读验证

```bash
# 不调用第三方模型：先核对真实 ID、标题、完整 JD
python3 liepin_apply.py --job "AI产品经理" --city 深圳 --experience 实习生 --degree 本科 \
  --count 2 --max-views 2 --max-pages 1 --dry-run --matcher keyword
python3 51job_apply.py --job "AI产品经理" --city 深圳 --job-type 实习 \
  --experience 在校生/应届生 --degree 本科 --count 2 --max-views 2 --dry-run --matcher keyword
# 27 届秋招校招岗位：工作类型用全职，工作年限用在校生/应届生
python3 51job_apply.py --job "AI产品经理" --city 深圳 --job-type 全职 \
  --experience 在校生/应届生 --degree 本科 --count 2 --dry-run --matcher keyword
python3 yupao_apply.py --job "AI产品实习" --city 深圳 \
  --count 2 --max-views 2 --dry-run --matcher keyword

# 已授权对应模型的数据外发并完成配置后，去掉 --matcher keyword 使用配置后端
```

`--count` 在 dry-run 中限制本轮评估岗位数（包含标题粗筛结论）。历史去重、缺 ID 和黑名单不计评估数量；详情另受 `--max-views` 限制，51job 的 views 表示列表 JD 评估。

| 筛选 | 猎聘 | 51job | 鱼泡 |
|---|---|---|---|
| 城市/关键词 | 支持 | 支持 | 支持 |
| 经验 | 原生点击 `workYearCode` 控件，如 实习生/应届生 | 原生点击工作年限选项；在校生、应届生、在校生,应届生均映射为 在校生/应届生 | 不支持 |
| 工作类型 | 待校准，可先用经验“实习生” | 原生点击，全职/兼职/实习 | 不支持 |
| 学历 | 原生点击，如 `--degree 本科`，只支持单个值 | 原生点击，如 本科 | 不支持 |

不支持或页面已变化的筛选明确报错，不静默忽略，不臆造参数。岗位是否满足条件仍须按完整 JD 评估。

猎聘学历可选：博士、硕士、本科、大专、中专/中技、高中、初中及以下；点击可见选项后校验选中结果。鱼泡无实习/学历/经验筛选（学历和经验下拉未触发列表重新请求，不能可靠筛选）；请用关键词（如“AI产品实习”）并依赖 Jev 硬条件，传入非空 `--job-type`、`--experience` 或 `--degree` 会明确报错。

## 模型与隐私

Jev 将**脱敏简历、求职偏好、职位标题/公司和完整 JD**发送到 `https://api.typesafe.ai/v1/systemone`；LLM 将相同资料发往配置的服务商。姓名等 `resume_redact` 指定字符串、手机号、邮箱会被替换；学校和项目等仍可能识别个人。使用前确认授权及脱敏边界。dry-run 在两种模型模式下也会真实请求和计费；keyword 不外发简历。

Jev 密钥只读环境变量 `JEV_API_KEY`，或仓库外 `~/.config/jobhunter/jev.env` 中的 `JEV_API_KEY=...`；LLM 凭证规则见上节。缺 key 或不可读简历时停止，不静默降级。

底层复用 `jev_match.decide`：学历、在校/应届、到岗/时长/城市三问取最小 `hard`，方向匹配用四档概率期望 `fit`。交接方案 B：hard < 0.2、明确建议跳过（概率 ≥ 0.6）或 fit < 1.5 时跳过；hard ≥ 0.6、fit ≥ 2.3 且未建议跳过时推荐，其余复核。

**新平台默认额外采用 `--match-policy review-gate`：Jev 建议复核时不自动投递。** 原始 Jev 结果与最终处理结论分开保存。`--match-policy boss-b` 可用于与交接方案 B 对照；BOSS 的阈值本次未改。

新平台缓存按平台、简历/偏好/问题/阈值版本摘要隔离，更新材料自动失效；`--refresh` 强制重评。猎聘/鱼泡命中 7 天缓存的复核/跳过不再打开详情；51job 始终只取列表接口 JD，先检查卡片申请/外跳状态及 JD；应届生每次先读取当前详情状态，再使用匹配缓存。真实提交前仍需核对当前岗位身份并重新检查 JD。缓存不写简历和 JD 原文。

## 人工复核与批准投递

先离线汇总日志中的 `review`，人工看过岗位后将对应行的 `[ ]` 改为 `[x]`（也识别 `[X]`）：

```bash
python3 review_list.py             # 全部城市
python3 review_list.py --city 深圳  # 仅该城市
```

`reports/复核清单.md` 按平台分节、按时间倒序，平台+ID 去重取最新记录。重新生成保留仍在清单中的勾选；任意城市日志中已 `applied/pending` 的 ID 自动剔除。旧 BOSS 记录缺少时间时，同 ID 按日志最后一条处理。清单属于个人投递资料，已加入 `.gitignore`；每轮覆盖的 `reports/队列/<平台>-<城市>-队列.md` 仍只用于查看本轮结论。

已批准后可运行以下命令；此处仅说明用法，**本阶段仅离线测试，未实投验证批准路径**：

```bash
python3 boss_apply.py --approved --city 深圳 --count 1
python3 liepin_apply.py --approved --city 深圳 --count 1
# 新平台或重新校准：锁定已勾选 ID，授权后只尝试一次
python3 yupao_apply.py --approved --city 深圳 --calibrate --count 1 --job-id 已确认的数字职位ID
python3 yingjiesheng_apply.py --approved --city 深圳 --count 1
```

`--approved` 不需要 `--job`，只处理该平台、该城市日志中且在清单打勾的 ID；URL 和标题以日志为准，修改清单链接不会改变导航目标。不搜索列表、不读匹配缓存、不调用 Jev，也不需要 Jev key 或简历。传 `--dry-run` 可只核对批准详情而不点击。

批准仅替代匹配判断：冷却、风控、黑名单、ID/公司去重、详情上限、沟通间隔和每五次成功休息仍生效；BOSS 批准模式拒绝 `--fast/--ignore-cooldown`，批准模式均不接受 `--fill-queue`。提交前先落盘 `pending`，未知结果停止且防重；成功记录 `applied`，原因以 `[人工批准]` 开头，下次生成清单时移除。

BOSS 直接打开日志详情 URL，点击前复核 URL 中的 ID、`.job-banner .name h1`（或 `h1`）与日志标题完全一致，并核对 `a.btn-startchat` 的 `data-url` 前缀与“立即沟通”文本；“继续沟通”记已沟通并跳过。猎聘、鱼泡复用 `same_job`、详情按钮核验和原生提交。未知详情结构进入复核，不点击。**51job 清单批准列为 `—`，`--approved` 直接报错，请在页面手动处理；未校准平台的首单门禁不变。**

## 首单校准与结果

猎聘、应届生、51job、鱼泡均已用真实首单校准（接口与页面双证据），见 `platform_adapters.VERIFIED_ENDPOINTS` 注释。未加入 `CALIBRATED` 的平台，普通真实批量运行会在连接浏览器前停止。**真实投递须先征得用户对该平台和岗位的明确授权**，首次使用或重新校准仅运行一份：

```bash
# 仅在只读验证、登录、匹配结论及用户授权均完成后运行
python3 liepin_apply.py --job "AI产品经理" --city 深圳 --experience 实习生 \
  --count 1 --max-views 5 --job-id 已确认的数字职位ID --calibrate
python3 yingjiesheng_apply.py --job "产品经理" --city 深圳 \
  --calibrate --job-id 已确认的数字职位ID --count 1
```

`--calibrate` 最多尝试一次，不自动确认额外弹窗，不自动发送自定义招呼语、不自动切换另一种投递动作。需要额外确认、返回未知结果或页面断连时保留 `pending`。若按钮成功但接口尚未识别，只记录页面证据；维护者应人工核对本次请求的端点、职位字段与成功码后添加精确协议并补测试，不能仅因一次点击自动解锁批量。

结果文件：

- `data/logs/<平台>-<城市>-log.json`：applied/pending/review/skipped/failed；风控为 `failed` 下 `status=blocked`。点击后风控仍保留 pending 防重。
- `reports/队列/<平台>-<城市>-队列.md`：本轮推荐、复核、跳过、数量和停止原因。推荐不等于投递成功。
- `reports/复核清单.md`：离线汇总待人工复核岗位，保留用户批准勾选。
- `data/cooldown/<平台>-cooldown.json`：冷却截止时间。
- `data/cache/jev-<平台>-cache.json` / `data/cache/llm-<平台>-cache.json`：对应模型缓存；BOSS 使用 `data/cache/jev-cache.json` / `data/cache/llm-cache.json`。

网络证据只记录去查询参数的 URL、HTTP 状态、code、职位关联标志与风控分类，不写请求正文、密钥和原始响应消息。应届生记录 `outer_status/nested_status` 短码；`/open/yjs/apply` 成功条件为二者均为 "1"，必须关联当前岗位。鱼泡页面域名为 `yupao.com`，业务证据同时收取 `yupaowang.com` 及其子域名；同域埋点不会因收取而成为成功证据，非证据域名仅记录 host/path。只 `applied` 计入成功；`pending` 需人工对照平台记录核验，不能直接删除后重跑。

## BOSS 现有入口

```bash
python3 boss_apply.py --job "AI产品经理" --city 深圳 --count 2 --dry-run
```

BOSS 默认 `--match-policy review-gate`：Jev 建议复核的岗位一律进队列，不自动沟通；`--match-policy boss-b` 恢复旧方案 B。

可选：投完后给 HR 未回复的会话补发带岗位名的招呼语（默认只预览，`--send` 才发送；公司+岗位核对一致才发，HR 已回复跳过，发过的记在 `data/boss-greet.json` 不重发）：

```bash
python3 boss_greet.py --city 深圳            # 预览
python3 boss_greet.py --city 深圳 --send --count 10
```

模板可在 `config.json` 用 `greeting_template` 配置，占位符 `{job}` `{company}` `{greeting}`。

BOSS 与新平台共用节奏常量、日志原子写入、平台 ID 索引和队列标题。`--fast` 等旧调试参数仅属于 BOSS，新平台不开放加速/跳过冷却。

## 回复日报（只读）

```bash
python3 inbox_report.py --city 深圳
python3 inbox_report.py --city 深圳 --no-liepin
```

连接已启动的 Chrome，新开标签读取 BOSS 消息列表和猎聘首页，结束关闭自己开的标签。**不点击任何会话、不输入、不发送**；打开 BOSS 会话会标为已读，因此只读取 `.friend-content` 列表 DOM。页面等待 5–7 秒；BOSS 最多向下滚动 5 次，每次间隔 1.5–3 秒，按公司+招聘者姓名+列表时间去重。日报仅覆盖本轮加载到的会话，不保证完整历史覆盖。

覆盖生成本地 `reports/回复日报.md`（已忽略提交），列出 HR 已回复、已读未回，仅送达只计数；猎聘仅显示未读数，请在猎聘网页手动查看。`--no-liepin` 显示“未读取”，不会当成 0。BOSS 状态元素缺失推断 HR 已回复，存在“已读”/“送达”分别归类，其余标为未知；这是列表最后一条消息的状态，不代表完整对话历史。

`--city` 仅选择 `data/logs/boss-<城市>-log.json` 的 applied 记录，不筛选会话。公司去空白、转小写匹配，列表品牌以 `...` 结尾时按前缀关联；品牌与日志公司名称不同可能匹配不到。未匹配标为“非本工具投递”，同公司多岗全部列出，不据此确认回复针对哪个岗位。匹配结果保留 `job`、`applied_at`（日志 `ts`）及 `applied_matches`；页面列表“职务”是招聘者职务，不当成投递岗位。

读取前后检查风控；命中立即退出码 1，仅复用冷却接口写 `data/cooldown/boss-cooldown.json`，不覆盖旧日报或修改投递日志。已有冷却时在连接前退出；读取失败也保留旧日报。本阶段仅通过离线替身验证，实际 DOM、列表滚动与未读状态仍需维护者实机确认。

## 代码与验证

- `platform_runner.py`：匹配、缓存、去重、队列、运行上限、提交状态。
- `config_tool.py`：首次配置、点路径修改、检查与自然语言字段映射。
- `llm_match.py`：OpenAI 兼容接口、JSON 答案校验与概率归一化。
- `platform_adapters.py`：搜索筛选、DOM 卡片、详情、按钮与标签生命周期。
- `apply_runtime.py`：节奏、风控、提交预记、监听和证据核验。
- `review_list.py`：离线复核清单与批准 ID 解析。
- `inbox_report.py`：只读回复日报，纯函数与页面读取分离。
- 共同运行器的四个平台 `*_apply.py`：保留城市表及 CLI 入口。
- `official_apply.py`：官网入口补全工具；日志、缓存与报告位置遵循新目录规则。

```bash
python3 -m pytest -q
```

测试使用离线替身，不能替代真实页面、真实模型和首单验收。[DrissionPage 监听文档](https://www.drissionpage.cn/browser_control/listener/)说明正常浏览器请求的监听方式。
