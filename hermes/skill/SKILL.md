---
name: reddit-monitor
description: >-
  Use for explicit Reddit content monitoring, candidate semantic review,
  Reddit Inbox/Notifications/Chat triage, natural English reply drafting,
  and explicitly authorized Reddit comment publishing via Camofox.
version: 2.2.0
license: private
metadata:
  hermes:
    category: social
    tags: [reddit, monitoring, inbox, replies, browser, camofox]
---

# Reddit Monitor (Hermes 优化版)

> **合规与授权声明**：本目录提供安全规程与状态契约，不构成用户授权、平台豁免或部署配置。任何在线访问与代发操作均须在运行前独立核实当前用户明确授权、固定 Camofox 登录身份、Reddit 最新官方政策（含 [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy)）及目标 Subreddit 板规。通过登录浏览器代发的单次或人工批准评论仍属 Agent 提交，须核验应用注册/`App` 标识、商业用途书面批准及私信联系同意等适用条件；缺证时区分「未核实」与「确认违规」，不启动或绕道执行受影响操作。

## 1. 触发条件与严格隔离边界

**仅在以下明确上下文中触发**（用户引用具体 `reddit-monitor` Cron 回执时，结合被引用对象判断范围）：
- Reddit / Subreddit / Custom Feed；
- Reddit 帖子、评论或 Permalink；
- Reddit Inbox / Notifications / Chat；
- `reddit-monitor` 任务或对应 Cron 回执；
- 明确要求起草、检查或发布 Reddit 回复。

**严禁触发场景**（脱离明确 Reddit 上下文的泛化短指令不触发，也不新建发布授权）：
- 单独的“检查消息”“检查并回复”“继续”“监控”；
- 电商买卖家消息、创作者商务合作私信、普通邮件；
- 通用浏览器登录调试或无关 Cron 管理。

唯一参考索引：[references/INDEX.md](references/INDEX.md)（按当前分支按需读取）。

---

## 2. 四大任务分支（严禁跨分支套用交付物）

默认只读，仅在获批配置指定的**同一 Camofox 登录身份**（`CAMOFOX_USER_ID` + 独立 `CAMOFOX_SESSION_KEY` + `REDDIT_EXPECTED_USER`）下操作，文档不提供默认 ID。

| 分支 | 职责与交付物 | 严禁越界行为 |
| :--- | :--- | :--- |
| **内容监控（Content Monitor）** | 预采集＋Agent 语义审核（`no_agent=false`）、`review` 落库、`summary` 核算、中文摘要（结尾固定 `已检查｜未起草｜未发布`） | 不起草、不发布、不检查 Inbox/Notifications/Chat |
| **Inbox 分流（Inbox Triage）** | 仅核查本轮独立授权通道（Old Reddit Unread / Notifications / Chat），报告 `需要回复 / 已处理 / 无需回复 / 未验证` 并用 `reddit_inbox_state.py` 落库 | 不从内容监控推导权限，不扫描内容 Feed |
| **手动起草（Draft）** | 核实实时帖子与现有评论，执行 `humanizer` 最终润色，输出英文草稿＋中文说明 | 草稿不等于已发布，未获明确授权不提交 |
| **授权发布（Publish）** | 核验本轮明确授权、实时复查评论与 `/about/rules`、唯一定位 `textarea[name="text"]` 回读、单次提交、Fresh Reload 五要素核验、落库防重 | 状态不明时严禁盲目重发；不切换身份或关闭整个 Profile |

---

## 3. 各模式执行规程

### 3.1 Read / Candidate Review（内容监控与候选审核）

详细计数与输出契约见 [monitoring-contract.md](references/monitoring-contract.md)。

1. **零回复全局分层优先**：
   - **主优先**：新发现、**零回复（`zero_comments`）**、问题明确且能给出可靠帮助的近期求助帖；
   - **次优先**：有评论但仅为机器人提示、作者自述补充或未回答核心问题的求助帖（严禁将非零评论改记为 `0`）；
   - **重要纠错/关键缺口例外**：多评论帖（或含危险误导的非求助帖）仅在原文存在具体安全风险或关键未解参数缺口且有可靠来源支持时例外推荐，须引用已有回答与剩余缺口；普通补充、买卖估价（主意图 `for sale` / `what is this worth`）、纯展示与泛泛讨论跳过。
   - **发现预算纪律**：零评论层全局先于非零/未知层；笼统求助探查位（最多 1 个）仅在同一评论层内保留，绝不挤占零回复帖；新候选先于旧帖重审（`retry`），无合适新帖时如实报告，不拿旧帖凑数。
2. **自定义 Feed 与领域配置**：
   - Custom Feed 固定 `/new/`，在同身份 Old Reddit 页面从 `.side ul.subreddits > li > a` 实时读取成员社区并逐条核对归属；成员读取失败、排序非 `new` 或账号不符立即失败退出。
   - 支持在 `state.json` 的 `watch[].domainTerms` 配置目标社区专属领域词（水路/泵阀/高压清洗专项排障规范按需读取 [domain-hardware-guide.md](references/domain-hardware-guide.md)）。
3. **语义审核统一五步法与视觉核查**：
   - 逐条记录：**核心问题 → 作者已提供/已尝试 → 现有回答 → 未解决要点 → 可核实且安全的新价值**。结论枚举：`recommended / not_recommended / needs_manual_review / failed_agent`。
   - **Old Reddit 媒体与看图边界**：
     - 评论内 `<image>` 文本链接：在同身份实时帖中仅读取该评论直属 `.entry .usertext-body a` 的 `href`（排除嵌套后代），交给 `vision_analyze`；
     - 相册漏报（`browser_get_images` 为空或仅站点图标）：用同身份 `browser_console` 只读提取 `.expando img` 与 `.expando a` 的真实 `src/href`，优先将原图 URL 交给 `vision_analyze`，严禁从缩略图凭空构造 URL，也不绕过已被安全层拒绝的资源；
     - 仅 `vision_analyze` 实际成功返回才标“已看图”；关键接口/螺纹/密封判断冲突或缺图时标 `needs_manual_review`，仅询问尚缺的型号/产品链接或可测尺寸，不让作者重拍已给过的同类照片。
4. **锁内落库与对账**：
   - 逐条通过获批运行时脚本的 `review --result-file <json>` 落库（写入 `lastReviewCandidateHash` SHA-256 指纹与状态快照）；
   - 审核完成后调用 `summary` 子命令在共享锁内对账扫描候选数、证据指纹与分组结果。

### 3.2 Inbox Triage（收件箱独立分流）

详细规程见 [inbox-triage.md](references/inbox-triage.md)。

- 分别核查 `old-reddit-messages`、`new-reddit-notifications`、`reddit-chat`：未验证记 `null + unverified`（Chat 仅加载 Shell 无会话列表时记 `unverified`，绝不报 `0`）。
- 仅具体技术追问、纠错或未解决问题标 `actionable`；打开完整线程确认本账号未在后续回复过；新追问按入站 `commentId` 独立记录，不降级已发布的 `posted_reply`。

### 3.3 Draft & Humanizer 最终润色（默认执行）

偏好见 [technical-reply-user-preferences.md](references/technical-reply-user-preferences.md)；水路/泵阀硬件细则见 [domain-hardware-guide.md](references/domain-hardware-guide.md)。

1. **技术与安全核实**：
   - 区分“原帖实际型号说明书要求”与“一般工程原理”；型号不明不移植同品牌其他型号的电流、回路、压力或拆装要求；区分空载极值（如零扬程最大流量、截止压力）与工作点参数；严禁建议带压拆接、敲击强接或移除安全保护装置。
   - 自然语气不等于虚构亲身购买或多年实测经历；涉及自家产品或利益相关推荐必须如实披露关联，普通技术帮助不强塞品牌。
2. **加载 `humanizer` Skill 执行最后润色**（若当前 Profile 不可用，明确说明限制并直接按下列规则人工润色，不自动改配置）：
   - 美式英语自然聊天语气，使用 contractions（`I'd`、`it's`、`won't`），通常 2–5 句保留一个核心判断与最有用的低风险下一步（安全提示需要时可超 5 句）；
   - 删除客服寒暄收尾（`It is important to`、`Additionally`、`Ultimately`、`I hope this helps`、`Let me know`）、固定三段式和空泛赞美，避免每条复用同一开头结尾；
   - **润色后逐项回对**：核对型号、接口、数值、单位、不确定性及安全边界未变，第一人称不伪装普通买家；
   - 默认只交付**最终英文草稿＋中文解释**（中间稿留内部）。

### 3.4 Publish（仅明确授权）

详细验证与排障见 [publishing-verification.md](references/publishing-verification.md)、[reddit-comment-composer-troubleshooting.md](references/reddit-comment-composer-troubleshooting.md) 与 [manual-posting-from-cron-review.md](references/manual-posting-from-cron-review.md)。

- **授权区分**：
  - 固定文本授权：提交前与批准稿逐字核对，实质修改须重新确认；
  - 明确委托选帖/起草并发布，或针对具体监控回执回复“检查并回复”：仅在该回执明确的推荐对象范围内完成实时复核、起草、润色与发布（不含历史待办或独立 Inbox，同源 Crosspost 只选一帖；若现场已被解答或证据不足则不发并说明）。
- **发布前**：
  1. 打开精确 Permalink，核对登录账号与目标帖子/评论；
  2. 重新核对实时评论数、正文与作者补充，同时读取 `/about/rules` 与侧栏板规；
  3. 精确绑定唯一可编辑 `textarea[name="text"]`：顶层校验隐藏域 `thing_id=t3_<post_id>`，嵌套校验目标评论 `t1_<comment_id>` 专属表单（排除后代表单、`input#shortlink-text[readonly]` 与 `disabled` 元素；若 `/type` 返回 500 `element is not editable` 按选错元素处理，不误报 Camofox 故障）；
  4. 回读 Composer 全文逐字核对一致后，仅点击该表单 `button.save[type="submit"]` 一次。
- **发布后**：
  1. Fresh Reload 精确评论链接，核对**作者、完整正文、Comment ID、Permalink 与直接 Parent** 五要素；
  2. 仅验证全部通过后报告 `已发布`，先存独立外部回执再锁内落库；
  3. 提交状态未知时先查远端线程，**严禁盲目重发**；仅清理本任务新建的非共享临时标签。

---

## 4. 网络、持久化与 Cron 失败契约

详见 [network-boundaries.md](references/network-boundaries.md) 与 [state-persistence.md](references/state-persistence.md)。

- **网络边界**：只读超时/429/5xx 在同身份同端点内有界重试并遵守 `Retry-After`；登录/身份/代理异常或结果不明时停止相关写入；公开 JSON/HTML/Atom 回退须另有明确批准，且绝不用于验证 Inbox/Chat 或发布结果。
- **持久化安全**：持共享锁完整读取最新文件，执行双阶段校验写（先校验缓存回读通过，再推进 `state.json` 批次键）；`contentRunId` 与 `inboxRunId` 独立维护；`posted / handled / archived` 终态受硬保护。附带 [持久化模板](scripts/scheduled_tick_persist_template.py) 仅限 `.persist-fixture-*` 隔离目录测试使用。
- **Cron 失败声明契约**：当内容任务采用“预采集脚本＋Agent”模式时，若采集全部失败或必需持久化/校验失败，核对本机调度器对 `[CRON_FAILURE]` 的支持，按当前失败契约在最终回复**第一行独立输出 `[CRON_FAILURE]`**（普通首行标题让位于该标记）；合法空候选、`needs_manual_review` 或仅首页覆盖不等于故障。

---

## 5. Verification Checklist

- [ ] 当前请求或会话延续已明确 Reddit 范围，发布授权已独立核实
- [ ] 实时上下文已核查；`load more`、折叠评论、未读图片或首页分页上限已如实标注
- [ ] Draft 与 Publish 状态严格区分；未知计数标记为 `null / unverified`
- [ ] 发布前已逐字回读 Composer 文本；发布后已完成 Fresh Reload 五要素验证
- [ ] 状态写入已在共享锁内完成双阶段原子替换与字段级回读核验
