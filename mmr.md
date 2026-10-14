# job-hunter 当前交接

## 2026-10-14 BOSS 实投状态（优先于下方旧记录）

- 用户授权现有 Jev 匹配及脱敏资料发送，目标新增 50 份；11:06 用户要求“先到这里”，已终止脚本并确认无残留进程。未经新指令不要继续。
- 本次新增已沟通 36 个唯一岗位/公司（AI产品经理搜索 17、产品实习 10、Agent 9）；全部接口 HTTP 200/code=0 与页面确认；pending/failed=0。历史原有 1 份不计本次，尚余目标 14。
- 仅核验发起沟通；未核验简历附件发送、HR回复或面试转化。
- 质量问题：36 份中 14 份 Jev 建议复核仍因当前阈值通过自动沟通；包含开发/算法岗位。代码和阈值未改，后续应先收紧方向与复核闸门。
- 本次证据、基线与用量报告：`/Users/yikuaibanz1/Desktop/bs/tmp/boss-apply-2026-10-14/`（投递结果.md、results.json、token-usage.json、run-01/02/03.txt）。

## 2026-10-14 其他平台升级（本次开发）

- 目标：猎聘、51job、鱼泡对齐 BOSS 的 ID 定位、Jev 匹配、只读验证、提交核验、去重与慢节奏。保留上述 BOSS 实投停止状态。
- 已在当前 `feat/p0-boss-verify` 工作区实现 `platform_runner.py`、`platform_adapters.py`、`apply_runtime.py`，三个入口共享流程。未 commit/push。
- 当前用户已确认：三个平台已登录；授权脱敏简历、求职偏好、岗位 JD 发往 `api.typesafe.ai` 用于本次实测；新平台采用更严格的 `review-gate`，Jev 建议复核一律进人工队列。BOSS 决策未改。
- 三个平台已各完成 2 条真实岗位 keyword dry-run；ID/标题/JD 可核对，0 投递。猎聘实习筛选、51job 实习/在校应届/本科筛选实测有效。早期 51job 测试暴露并修复了标签归属竞态，按 openerId 管理本次详情。
- 新平台缺少真实首单投递端点校准：普通真投启动时拒绝；用户授权具体候选后才可 `--calibrate --count 1 --job-id <已确认ID>`，最多尝试一次。不得把只读页面验证说成真实投递通过。
- 真实 Jev dry-run 已完成 9 岗：推荐 3、复核 5、标题跳过 1，0 投递；原始概率与实际处理分开保留。猎聘候选 1985442155 已人工复核 JD 和按钮，用户已明确授权仅该岗沟通一次并已完成：16:01:43，按钮“聊一聊”→“继续聊”，记录 applied=1/pending=0，已进 ID/公司去重。网络 open-chat 等 HTTP 200，但未读出业务成功字段或职位关联，不能声称双证据通过；不再点击该岗位，不开启批量。证据 `evidence/liepin-first-calibration-2026-10-14.json`。更详细的状态和证据见 `evidence/platform-validation-2026-10-14.md`；规格见 `evidence/platform-upgrade-spec.md`。
- 日志点击前预写 pending；不明结果、断连/中断都防重且停止。证据 URL 去查询参数，不存请求正文或原始响应消息。运行只用真实浏览器 UI，不重放内部 API。
- 鱼泡的经验/工作类型/学历与猎聘学历筛选待校准，未知选项直接报错。51job 外部招聘平台链接跳过，不自动提交。
- 验证：389 项离线回归通过，`git diff --check` 通过。使用：README.md / SKILL.md；离线回归：`python3 -m pytest -q`。Jev 模式仍会外发脱敏资料和计费，keyword 不会。

## 2026-10-14 下午（Claude）

- BOSS 加 `--match-policy`（默认 review-gate，复用 platform_runner.effective_decision）。`config.greeting` 删去“想应聘AI产品实习”。
- 新增可选 `boss_greet.py`：已沟通且 HR 未回复的会话补发带岗位名招呼语；16:35 对不负时光「AI研发实习生」实发 1 条，对话区确认送达。
- 鱼泡 438856286 首单校准：按钮“发送简历”→“继续聊”，applied；未抓到 yupao.com 业务包，已加 other_urls（只记 host+path）诊断，下次校准定位端点。鱼泡仍未解锁批量。
- 猎聘 1975191697 第二次校准：open-chat HTTP 200、flag=1，请求 jobId=75191697（页面 ID 去掉前缀 "19"）。已登记 VERIFIED_ENDPOINTS + 别名关联，猎聘 CALIBRATED。
- 51job：`--experience` 选项全名是“在校生/应届生”。dry-run 中途弹层风控，冷却至 18:39；本次无 apply 候选（3 复核 + 2 匹配不足）。
