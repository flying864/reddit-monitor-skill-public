# 内容监控计数与输出契约

## 1. 最小证据与执行/覆盖分离

每轮监控必须如实记录 `contentRunId`、带时区的 `startedAt` / `finishedAt`、请求 Watch 数、成功/失败来源、分页上限与上下文缺失原因。

| 字段 | 枚举 / 类型 | 严格语义 |
| :--- | :--- | :--- |
| `executionStatus` | `success` / `partial` / `failed` | `success`：所有 Watch 列表及选中线程均成功读取；`partial`：列表或线程部分成功部分失败（某 Watch 合法为空、另一 Watch 失败计为 `partial`）；`failed`：所有列表失败，或本轮所有尝试读取的正文全部失败。 |
| `coverageComplete` | `boolean` | 仅当目标时间窗、分页边界与全部评论上下文均完整无截断时为 `true`。默认仅抓首页前 25 条时必须为 `false`，即使 `executionStatus="success"` 也须明确说明“仅覆盖首页前 25 条”。 |
| `discussion_status` | `zero_comments` / `has_comments` / `unknown` | 评论数缺失、非整数或存在未验证截断时必须记为 `unknown`（JSON 计数用 `null`），严禁用 `0` 伪装。 |

---

## 2. 零回复优先选帖与双阶段计数口径

### 列表发现阶段（Listing-Time）
- 将关键词命中（`keyword`）与笼统求助线索（`generic_help`，如求助标题或 Help/Question Flair）合并排序：**`zero_comments` 层全局优先于非零/未知层**。
- 泛求助探查名额（最多 1 个保留位）仅在当前轮到的同一评论层内生效，绝不允许有回复的泛求助帖跨层挤占零回复帖预算。
- 自定义 Feed（`/user/<u_name>/m/<feed>/new/`）必须固定 `/new/` 排序，每轮实时从 `.side ul.subreddits > li > a` 提取成员社区集并校验帖子归属；成员列表为空或排序不符立即失败退出，拒绝沿用过期范围。
- 列表阶段统计字段：
  - `scan.newLeadDiscussionCounts`：去重后新发现线索
  - `scan.newSelectedDiscussionCounts`：预算内选中读取正文的线索
  - `scan.newDeferredDiscussionCounts`：超出每轮 `maxCandidates` 预算未读取的线索（报“本轮未读”，不承诺下轮必然在首页）

### 正文读取与候选阶段（Context-Time）
正文读取后重新按标题＋正文计算关键词与相关性（支持 `watch.domainTerms` 自定义领域词），严格对账：
$$\text{newSelected} = \text{newCandidates} + \text{newPrefilterSkipped} + \text{newContextDedup} + \text{newContextFailures}$$

- `newCandidateDiscussionCounts` / `retryCandidateDiscussionCounts`：入选待语义复核的新候选与旧帖重试候选
- `newPrefilterSkippedDiscussionCounts`：正文初筛确认买卖/估价/展示/无关（`skipped_low_fit`）
- `newContextDedupDiscussionCounts`：正文阶段才暴露跨帖/同源关系而显式去重（`skipped_duplicate_thread`，**不计入正文失败，不消耗 `retryCount`**）
- `newContextFailureListingCounts`：正文读取异常（`skipped_context_unverified`，保留后续有界重试资格）
- `newListingZeroChangedAtContextCount`：列表时为 0 评论、读取正文时已出现评论的帖子数

---

## 3. 语义复核与 `summary` 对账

1. 候选语义复核逐条通过 `python3 hermes/runtime/reddit_monitor_tick_noagent.py review --result-file <path>` 落库。
2. 全部复核完成后，调用 `python3 hermes/runtime/reddit_monitor_tick_noagent.py summary` 在共享锁内只读输出 `outcomesByOriginAndContextDiscussion`：
   - 自动校验 `state.contentRunId == cache.contentRunId`；
   - 自动将 `scan.newCandidateCount` / `scan.retryCandidateCount` 及分组计数与 `cache.candidates` 逐项对账，防止候选被删改后静默汇总；
   - 自动校验每条已审记录的 `lastReviewCandidateHash`（SHA-256）与 `lastReviewSnapshot`，发现证据或状态篡改立即非零退出。

---

## 4. 分支回执输出模板

- **内容监控回执**：
  1. 首行结论：明确区分**实质零回复/未解决求助推荐数**与**安全纠错例外数**，提示是否需要用户进一步决策；
  2. 扫描元数据：`contentRunId`、起止时间与时区、Watch 来源、执行状态与覆盖范围限制；
  3. 队列分流计数：新发现候选 / 旧帖重试 / 预算延期 / 人工待办（`manual_pending`）；
  4. 候选复核明细：推荐、跳过、信息不足待复核（`needs_manual_review`）、复核失败及关键 Permalink 证据；
  5. 持久化校验状态与固定尾部声明：`已检查｜未起草｜未发布`（不含回复草稿与 Inbox 清单）。
- **Inbox 分流回执**：仅报告授权通道验证状态、`actionable / handled / noise / deferred` 计数、本轮授权内实际动作及持久化校验结果；未验证通道明确标注“未验证”，不报 `0`。
- **手动草稿 / 发布回执**：草稿模式输出英文定稿＋中文说明；发布模式输出精确评论链接、提交英文原文、中文大意及五要素回读核验结果。
