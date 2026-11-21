# 当前 Inbox 分流

默认只读；沿用主 SKILL 授权边界，明确 Reddit 上下文中的继续不必重新问，但不创建长期自动发送权。

分别核查 old_reddit_unread、notifications、chat，记录 status、checked_at、source、unread_count；未验证为 null + unverified，错误另列 reason。Chat 只有 shell、badge 陈旧或公开 Feed 为空均不能证明通道为空。只报告当前可读取范围，不把某一通道成功概括为全部成功。

Actionable 需有具体追问、纠错或未解决问题。打开完整线程核对目标 comment ID、父链、发信人已尝试的事实与本账号后续回复。简短感谢、成就、无新问题的更新一般跳过。已有回复记录仍需结合实时线程，新追问单独用 comment ID 建项，不降级原 posted/handled 记录。

草稿和发布分离；发布及发布后状态记录使用[发布验证](publishing-verification.md)和[状态持久化](state-persistence.md)，不从缓存的 actionable=true 推导权限。
