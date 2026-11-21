# 从监控回执进入手动/委托回复路由

本页说明当用户引用某次 Reddit 内容监控简报并下达操作指令（例如回复“检查并回复推荐帖”）时的标准衔接流程。

## 执行四步法

1. **锁定授权边界**：
   - 从用户引用的监控简报中提取推荐目标 Permalink 清单与对应 `contentRunId`；
   - 仅覆盖该简报明确列出的推荐项（若同作者/Crosspost 存在重复线程，仅选最合适的一个代表线程）；
   - **不自动纳入**历史人工待办（`manual_pending`）、已跳过项或独立的 Inbox/Chat 消息。
2. **实时复核、起草与发布**：
   - 按 [publishing-verification.md](publishing-verification.md) 打开实时页面，重新核对最新评论区、作者补充与目标 Subreddit 板规；
   - 若现场已被其他网友高质量解答或关键规格仍不足以安全作答，主动停止该帖发布并向用户如实解释，绝不为了凑任务强行发帖；
   - 通过核查后按主 Skill 起草自然美式英语回复、完成去 AI 腔润色、定位表单回读（参考 [reddit-comment-composer-troubleshooting.md](reddit-comment-composer-troubleshooting.md)）、单次提交并执行 Fresh Reload 五要素核验。
3. **保存回执与局部状态更新**：
   - 先保存包含精确正文、账号、`commentId`、Parent、Permalink 与验证时间的独立发布回执；
   - 按 [state-persistence.md](state-persistence.md) 在共享锁内更新对应 `seen.byPermalink` 条目为 `posted_manual` / `posted`，保留原批次扫描统计与 `lastReviewPayload` 历史不变。
4. **交付反馈**：
   - 清晰列出每条目标的最终状态（已发布 / 现场已解决跳过 / 阻断未发）、精确评论链接、实际发布的英文原文及简洁中文释义。
