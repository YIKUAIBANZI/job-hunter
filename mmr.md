# job-hunter 当前交接

- 主线：在保留 Boss P0 真实沟通核验与慢节奏的前提下，用 Jev 做 JD × 脱敏简历三问分流；其他平台继续 score_jd。
- 当前分支：`feat/p0-boss-verify`；P1 为本地未提交改动，不 commit / push。
- 实现：`jev_match.py` 管理脱敏、key 读取、请求、阈值决策、缓存和纯队列渲染；`boss_apply.py` 接入 matcher、review、失败停止及退出队列；`shared.py` 补配置默认值、review bucket 和 Boss 可选参数。
- 验证：`python3 -m pytest -q` 离线通过 112 项；`python3 boss_apply.py --help` 正常。未真实调用 Jev、未连接 Boss、未发起沟通。
- 下一步：用 `--dry-run` 人工对照 985/211 硬要求、正文“资深导师”、AI 产品销售等真实 JD；确认队列和隐私脱敏后，验证真实 Boss P0 核验与节奏。
- 约束：key 仅从环境变量或仓库外 `~/.config/jobhunter/jev.env` 读取，禁止输出或入库。真实 Jev 会发送脱敏简历、JD 与求职偏好。缓存只按职位 ID + JD 失效，更新简历/偏好/问题后需清缓存。
- 使用：README.md / SKILL.md；离线回归：test_jev_match.py / test_boss_match.py / test_shared.py。
