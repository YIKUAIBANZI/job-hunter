# Job Hunter — AI 辅助求职

用本地 Chrome 读取招聘岗位，按简历和偏好筛选，再通过浏览器点击提交并核验。支持 BOSS、猎聘、51job、鱼泡；不会重放平台内部接口，不处理或绕过验证码。

## 当前能力与边界

BOSS 保留现有已实测流程。猎聘、51job、鱼泡已接入共同运行器，**仍处于上线校准阶段，不能把离线测试或只读浏览当成投递成功**。逐平台证据见 [验证记录](evidence/platform-validation-2026-10-14.md)。

三个新平台默认 `--matcher jev`，也支持完全本地的 `--matcher keyword`。`--dry-run` 实际阻止所有投递和聊天动作。关键词模式只是回退/对照，可能把销售、研发岗位判高分，也可能因正文出现“资深”误杀实习岗位。

共同流程包含：

- 平台职位 ID 定位、卡片与详情 ID/标题核对；未知身份进入复核。
- Jev 标题粗筛、完整 JD 匹配、7 天结论缓存、复核队列。
- `<平台>:<id>` 与公司+标题双去重，`pending` 也阻止重复提交。公司未知时只按 ID 去重，正式提交前必须取得公司信息。
- 点击前原子写入 `pending`，监听 XHR/Fetch 后只原生点击一次。断连、超时、未知结果不重试，停止本轮。
- 仅已校准端点且请求关联当前职位的响应可确认网络成功。未知接口 `code=0` 无效；当前岗位按钮在点击后变为明确成功状态也可确认。两路证据分别保存，不把其中一路伪称双证据。
- 页面/接口出现风控立即停，写入该平台 2 小时冷却。登录或简历向导需要人工处理。
- 猎聘/鱼泡详情间隔 6–15 秒，每 10 份额外休息 45–90 秒，默认最多 60 份详情。51job 风控更敏感：详情间隔 15–30 秒，每 5 份额外休息 90–180 秒，默认最多 20 份详情；显式 `--max-views` 优先（1–60）。各平台投递后 30–90 秒，每 5 次成功额外休息 3–5 分钟，默认最多 5 页搜索，投满停止。节奏不保证免受风控。
- 搜索标签复用；详情标签按 `openerId` 确认归属并清理。避免同时运行多个投递脚本或手动操作正在自动浏览的同一搜索页。

## 安装与启动

需要 Python 3.10+、DrissionPage 4.x，以及 Chrome：

```bash
python3 -m pip install DrissionPage pytest

# macOS；此目录保留平台登录状态，不要上传
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/chrome-debug"
```

在该 Chrome 中手动登录目标平台。51job 还需填写在线简历。

复制 `config.example.json` 为 `config.json`，配置 UTF-8 文本简历路径、`job_profile`、`resume_redact`、公司黑名单等。不要将个人配置、简历、日志、浏览器目录或密钥提交到 Git。

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

# 已授权 Jev 数据外发并配置 key 后，将 --matcher keyword 改为 --matcher jev
```

`--count` 在 dry-run 中限制本轮评估岗位数（包含标题粗筛结论）。历史去重、缺 ID 和黑名单不计评估数量；详情另受 `--max-views` 限制。

| 筛选 | 猎聘 | 51job | 鱼泡 |
|---|---|---|---|
| 城市/关键词 | 支持 | 支持 | 支持 |
| 经验 | 原生点击 `workYearCode` 控件，如 实习生/应届生 | 原生点击工作年限选项；在校生、应届生、在校生,应届生均映射为 在校生/应届生 | 不支持 |
| 工作类型 | 待校准，可先用经验“实习生” | 原生点击，全职/兼职/实习 | 不支持 |
| 学历 | 原生点击，如 `--degree 本科`，只支持单个值 | 原生点击，如 本科 | 不支持 |

不支持或页面已变化的筛选明确报错，不静默忽略，不臆造参数。岗位是否满足条件仍须按完整 JD 评估。

猎聘学历可选：博士、硕士、本科、大专、中专/中技、高中、初中及以下；点击可见选项后校验选中结果。鱼泡无实习/学历/经验筛选（学历和经验下拉未触发列表重新请求，不能可靠筛选）；请用关键词（如“AI产品实习”）并依赖 Jev 硬条件，传入非空 `--job-type`、`--experience` 或 `--degree` 会明确报错。

## Jev 与隐私

Jev 将**脱敏简历、求职偏好、职位标题/公司和完整 JD**发送到 `https://api.typesafe.ai/v1/systemone`。姓名等 `resume_redact` 指定字符串、手机号、邮箱会被替换；学校和项目等仍可能识别个人。使用前确认授权及脱敏边界。dry-run 在 Jev 模式下也会真实请求和计费；keyword 不外发简历。

密钥只读环境变量 `JEV_API_KEY`，或仓库外 `~/.config/jobhunter/jev.env` 中的 `JEV_API_KEY=...`。缺 key 或不可读简历时停止，不静默降级。

底层复用 `jev_match.decide`：学历、在校/应届、到岗/时长/城市三问取最小 `hard`，方向匹配用四档概率期望 `fit`。交接方案 B：hard < 0.2、明确建议跳过（概率 ≥ 0.6）或 fit < 1.5 时跳过；hard ≥ 0.6、fit ≥ 2.3 且未建议跳过时推荐，其余复核。

**新平台默认额外采用 `--match-policy review-gate`：Jev 建议复核时不自动投递。** 原始 Jev 结果与最终处理结论分开保存。`--match-policy boss-b` 可用于与交接方案 B 对照；BOSS 的阈值本次未改。

新平台缓存按平台、简历/偏好/问题/阈值版本摘要隔离，更新材料自动失效；`--refresh` 强制重评。命中 7 天缓存的复核/跳过不再打开详情；真实提交前仍需读取当前详情、核对身份并重新检查 JD。缓存不写简历和 JD 原文。

## 首单校准与结果

已校准平台见 `platform_adapters.CALIBRATED`：猎聘已完成两次首单（`open-chat` 返回 `flag=1` + 按钮变“继续聊”）；51job、鱼泡尚未校准（鱼泡 1 次仅页面证据），普通真实批量运行会在连接浏览器前停止。**真实投递须先征得用户对该平台和岗位的明确授权**，随后仅运行一份：

```bash
# 仅在只读验证、登录、匹配结论及用户授权均完成后运行
python3 liepin_apply.py --job "AI产品经理" --city 深圳 --experience 实习生 \
  --count 1 --max-views 5 --job-id 已确认的数字职位ID --calibrate
```

`--calibrate` 最多尝试一次，不自动确认额外弹窗，不自动发送自定义招呼语、不自动切换另一种投递动作。需要额外确认、返回未知结果或页面断连时保留 `pending`。若按钮成功但接口尚未识别，只记录页面证据；维护者应人工核对本次请求的端点、职位字段与成功码后添加精确协议并补测试，不能仅因一次点击自动解锁批量。

结果文件：

- `<平台>-<城市>-log.json`：applied/pending/review/skipped/failed；风控为 `failed` 下 `status=blocked`。点击后风控仍保留 pending 防重。
- `<平台>-<城市>-队列.md`：本轮推荐、复核、跳过、数量和停止原因。推荐不等于投递成功。
- `<平台>-cooldown.json`：冷却截止时间。
- `jev-<平台>-cache.json`：新平台缓存；BOSS 保留 `jev-cache.json`。

网络证据只记录去查询参数的 URL、HTTP 状态、code、职位关联标志与风控分类，不写请求正文、密钥和原始响应消息。鱼泡页面域名为 `yupao.com`，业务证据同时收取 `yupaowang.com` 及其子域名；同域埋点不会因收取而成为成功证据，非证据域名仅记录 host/path。只 `applied` 计入成功；`pending` 需人工对照平台记录核验，不能直接删除后重跑。

## BOSS 现有入口

```bash
python3 boss_apply.py --job "AI产品经理" --city 深圳 --count 2 --dry-run
```

BOSS 默认 `--match-policy review-gate`：Jev 建议复核的岗位一律进队列，不自动沟通；`--match-policy boss-b` 恢复旧方案 B。

可选：投完后给 HR 未回复的会话补发带岗位名的招呼语（默认只预览，`--send` 才发送；公司+岗位核对一致才发，HR 已回复跳过，发过的记在 `boss-greet.json` 不重发）：

```bash
python3 boss_greet.py --city 深圳            # 预览
python3 boss_greet.py --city 深圳 --send --count 10
```

模板可在 `config.json` 用 `greeting_template` 配置，占位符 `{job}` `{company}` `{greeting}`。

BOSS 与新平台共用节奏常量、日志原子写入、平台 ID 索引和队列标题。`--fast` 等旧调试参数仅属于 BOSS，新平台不开放加速/跳过冷却。

## 代码与验证

- `platform_runner.py`：匹配、缓存、去重、队列、运行上限、提交状态。
- `platform_adapters.py`：搜索筛选、DOM 卡片、详情、按钮与标签生命周期。
- `apply_runtime.py`：节奏、风控、提交预记、监听和证据核验。
- 三个平台的 `*_apply.py`：保留城市表及 CLI 入口。
- `official_apply.py`：原有官网入口补全工具，本次未改。

```bash
python3 -m pytest -q
```

测试使用离线替身，不能替代真实页面、真实模型和首单验收。[DrissionPage 监听文档](https://www.drissionpage.cn/browser_control/listener/)说明正常浏览器请求的监听方式。
