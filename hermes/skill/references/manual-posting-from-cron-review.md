# 从监控回执进入手动回复

此页仅作工作流路由，避免维护另一套发布和状态协议。

1. 根据用户引用的监控回执识别推荐对象及原 contentRunId；“检查并回复”的范围按主 SKILL 的 Publish 分支解释，不包含历史待办或独立 Inbox。
2. 按 [publishing-verification.md](publishing-verification.md) 实时核对原帖、现有回答和授权，再起草、润色、提交与 fresh reload 验证。评论框有歧义时使用 [reddit-comment-composer-troubleshooting.md](reddit-comment-composer-troubleshooting.md)。
3. 按 [state-persistence.md](state-persistence.md) 保存真实发布回执、标记精确目标。保留原始扫描/审核历史；不删除已发布候选来改变已完成批次计数，不给无关 Inbox 或 URL 变体批量写 handled。
4. 返回发布状态、精确评论链接、英文原文与中文意思；没有新增价值或提交前条件无法核实时明确未发；提交后无法验证则报告结果未知，不声称未发，不为完成请求凑回复。
