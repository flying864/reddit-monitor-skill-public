# Reddit Monitor for Hermes

用于在 Hermes 中审核 Reddit 新帖、候选语义复核、分流收件箱以及辅助经授权评论回读验证的 Skill 与配套运行时（v2.2.0）。采集器默认纯只读：不发送评论，也不读取收件箱。收件箱检查及任何回复操作均需另行获得明确授权，并满足 Reddit 官方政策及目标社区板规。

- [使用说明、配置、一键升级与离线测试](hermes/README.md)
- [Skill 入口](hermes/skill/SKILL.md)
- [主题参考索引](hermes/skill/references/INDEX.md)
- [采集脚本、跨平台锁与回归测试套件](hermes/runtime/)
- [Hermes 安全升级脚本](hermes/deploy_to_hermes.py)

### v2.2 主要改进

- **跨平台零依赖运行**：新增 `file_lock.py` 兼容 POSIX (`fcntl`) 与 Windows (`msvcrt`) 非阻塞独占文件锁及安全原子校验写，修复跨平台运行与编码兼容性问题。
- **消除关键词子串误报与领域解耦**：`word_hit` 强制全词边界匹配，支持在 `state.json` 的 `watch[].domainTerms` 按社区自定义领域术语，并将水路/泵阀硬件细则解耦为按需加载参考。
- **补全状态一致性与归档闭环**：修复正文阶段同源跨帖去重计数、`summary` 扫描计数与 SHA-256 证据指纹漂移校验、Inbox `A→B→A` 防回拨及 URL 别名保护，新增 `archive` 与 `init` 子命令。

本仓库只包含通用代码、说明和合成测试数据。无需 Reddit 账号即可运行全部 85 项离线测试；请勿将自己的账号、登录态、环境变量实际值或运行数据提交到 Git。公开可见不等于授予开源许可证。
