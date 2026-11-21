# Reddit Monitor 参考文档索引

## 查阅原则

1. 先读 [主 SKILL.md](../SKILL.md) 明确当前任务分支（内容监控 / Inbox 分流 / 回复起草 / 授权发布）。
2. **按需加载**：仅读取当前分支涉及的专题文件，保持上下文精简。
3. 本目录定义安全规程与状态契约，不构成线上账号授权。

## 专题文档目录

| 主题 | 文档 | 适用场景 |
| :--- | :--- | :--- |
| **监控计数与输出契约** | [monitoring-contract.md](monitoring-contract.md) | 定时/手动内容扫描、零回复分层、候选计数对账、`summary` 输出规范 |
| **状态持久化与防重契约** | [state-persistence.md](state-persistence.md) | `state.json` / 缓存双阶段写入、重放漂移检测、Inbox 水位保护、显式归档（`archive`） |
| **Inbox / Chat 分流** | [inbox-triage.md](inbox-triage.md) | Old Reddit Unread、New Notifications、Reddit Chat 三通道独立核查与落库 |
| **发布与嵌套回复验证** | [publishing-verification.md](publishing-verification.md) | 发布前授权/板规检查、父节点校验、Fresh Reload 五要素回读 |
| **评论框定位与防误触** | [reddit-comment-composer-troubleshooting.md](reddit-comment-composer-troubleshooting.md) | Old Reddit `textarea[name="text"]` 唯一定位、排除只读框与防重复点击 |
| **监控回执转手动回复** | [manual-posting-from-cron-review.md](manual-posting-from-cron-review.md) | 用户针对监控简报回复“检查并回复”时的范围界定与状态回写 |
| **网络边界与重试策略** | [network-boundaries.md](network-boundaries.md) | 同身份重试、`Retry-After` 遵守、浏览器会话边界与公开回退审批要求 |
| **技术看图与草稿偏好** | [technical-reply-user-preferences.md](technical-reply-user-preferences.md) | 图片不可读处理、接口不确定性披露、英中双语交付约定 |
| **流体/泵阀硬件专项指南** | [domain-hardware-guide.md](domain-hardware-guide.md) | 高压清洗/快接密封/隔膜泵/软管压降等硬件社区专项排障规范（按需选用） |

## 配套可执行脚本

- [scheduled_tick_persist_template.py](../scripts/scheduled_tick_persist_template.py)：随本 Skill 分发的隔离持久化测试模板（仅限 `.persist-fixture-*` 目录使用，非生产 Cron 入口）
- 生产运行时程序（位于 `hermes/runtime/`，部署至线上 Cron 脚本目录，三者须同目录存放）：
  - `reddit_monitor_tick_noagent.py`：内容采集与本地状态 CLI（`init` / `run` / `review` / `summary` / `archive`）
  - `reddit_inbox_state.py`：Inbox 状态校验与幂等落库 CLI（`status` / `persist`）
  - `file_lock.py`：跨平台（POSIX `fcntl` / Windows `msvcrt`）非阻塞锁与原子校验写公共模块
