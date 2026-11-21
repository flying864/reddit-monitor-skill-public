# Reddit Monitor 唯一参考索引

## 优先级与范围
当前明确授权、适用平台条件和获批运行配置独立核验；本目录不授予操作权限。查阅顺序：[主 SKILL.md](../SKILL.md) → 下列当前主题。只修改 Skill 不等于已修改或验证线上 Cron。

## 当前主题（按需读取）
- 技术看图、不猜接口及草稿复核：[technical-reply-user-preferences.md](technical-reply-user-preferences.md)
- 监控范围、分页、缓存与输出：[monitoring-contract.md](monitoring-contract.md)
- Inbox / Notifications / Chat 分流：[inbox-triage.md](inbox-triage.md)
- 发布、嵌套定位与回读：[publishing-verification.md](publishing-verification.md)
- 评论框定位、同会话填入与防重复提交：[reddit-comment-composer-troubleshooting.md](reddit-comment-composer-troubleshooting.md)
- 从监控回执进入手动回复的路由：[manual-posting-from-cron-review.md](manual-posting-from-cron-review.md)
- 状态、幂等与隔离模板：[state-persistence.md](state-persistence.md)
- 网络、同身份重试与经批准回退：[network-boundaries.md](network-boundaries.md)

[持久化模板](../scripts/scheduled_tick_persist_template.py) 仅用于隔离夹具，不是生产 Cron 入口。运行时采集与 Inbox 辅助程序另放在仓库的 `hermes/runtime/`，不随本 Skill 分发。
