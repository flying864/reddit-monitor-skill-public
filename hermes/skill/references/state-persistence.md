# 状态持久化、防重与归档契约

## 1. 运行数据目录与文件职责

所有生产运行状态必须存放在代码仓库外的独立目录（由环境变量 `REDDIT_MONITOR_DIR` 指定），共享同一把非阻塞文件锁 `<REDDIT_MONITOR_DIR>/run.lock`（通过 `file_lock.py` 兼容 POSIX `fcntl` 与 Windows `msvcrt`）。

| 任务域 | 批次键 | 所属文件集合 | 隔离约束 |
| :--- | :--- | :--- | :--- |
| **内容监控与复核** | `contentRunId` | `state.json`（顶层 `contentRunId`、`seen.byPermalink`）↔ `review-cache/latest.json` | 绝不修改 `state.json.inbox` |
| **Inbox 分流** | `inboxRunId` | `state.json.inbox` ↔ `inbox-cache/latest.json` ↔ `inbox-cache/deferred.json` | 绝不修改 `contentRunId` 与 `seen` |
| **显式归档审计** | 继承当前 `contentRunId` | `archive-journal/<journalId>/{state.before.json, cache.before.json, journal.json}` | 仅追加 `entry.archive` 标记并清理对应 `manual_pending` |

> **跨任务独立性**：`contentRunId` 与 `inboxRunId` 各自独立推进，不要求相等；每个工具仅校验自身所属文件集合的同名批次键一致性。

---

## 2. 双阶段原子写入与中断保护

多次单文件 `os.replace` 不构成跨文件数据库事务，因此必须遵守以下写入顺序：
1. 持非阻塞独占锁完整读取最新 JSON（禁止基于截断或分页片段覆盖）；
2. 写入第一份文件（如 `review-cache/latest.json`），立即完整回读并逐字段校验一致后，**才允许**推进 `state.json` 的批次键并执行原子替换；
3. 若任一侧批次键缺失或不一致（`check_run_pair` / `inboxRunId` 校验失败），立即拒绝读写并报告中断，严禁静默合并掩盖不一致。

---

## 3. 幂等重放、防回拨与终态保护

1. **终态硬保护**：
   - 内容侧：`posted`、`posted_manual`、`posted_reply`、`handled`、`already_replied`（无论位于顶层还是 `meta`）不可被扫描、草稿或 `review` 覆盖；
   - Inbox 侧：以**稳定入站 ID**（`parentCommentId` / `messageId` / `chatMessageId` / 入站 `commentId`）为主键，URL 仅作归一化别名。同一入站目标即使更换 URL slug 提交 `no_reply` 或 `already_replied`，也不得覆盖或重复创建已有的 `posted_reply` 记录。
2. **内容复核指纹校验（`review` & `summary`）**：
   - `review` 写入 `lastReviewIntegrityVersion=1`、`lastReviewCandidateHash`（候选稳定序列化 SHA-256）及 `lastReviewSnapshot`；
   - 同批次同载荷重放若状态字段或缓存候选证据发生任何漂移，立即抛出异常拒绝；同批次不同载荷直接拒绝。
3. **Inbox 跨批防回拨（`A → B → A` 防护）**：
   - `state.json.inbox._runHistory` 记录历史批次摘要：当新批次 `B` 已推进水位后，重放旧批次 `A` 会被明确拒绝（`stale inbox batch replay rejected`），防止水位回拨；
   - `postedReplies` 要求对应来源通道状态必须为 `verified`，且单批内禁止同一入站目标重复登记多条回复证据。

---

## 4. 显式归档生命周期（`archive` 子命令）

针对超过复核上限或时间窗的 `manual_pending` 条目，仅在获得**用户针对精确链接的明确批准**后，调用：

```bash
python3 hermes/runtime/reddit_monitor_tick_noagent.py archive --archive-file archive_req.json
```

`archive_req.json` 示例：
```json
{
  "userApproved": true,
  "contentRunId": "reddit-20261122T000000Z-a1b2c3d4",
  "reason": "用户确认归档超期历史人工待办",
  "targets": [
    {
      "permalink": "https://old.reddit.com/r/example/comments/abc123/help/",
      "expectedSha256": "<可选：当前 seen 条目指纹>"
    }
  ]
}
```

- **生命周期语义**：写入 `entry.archive = {"status": "archived", "automaticReview": false, "reminders": false, ...}`，保留原 `action`、`reviewStatus` 与 `reviewNotes` 历史证据，不伪装成“问题已解决”。
- **防误操作保护**：自动拒绝当前轮活跃候选（`cache.candidates`）与已发布保护条目，并在修改前写入不可变 `archive-journal/<journalId>/` 备份与日志。

---

## 5. 隔离持久化模板说明

[scheduled_tick_persist_template.py](../scripts/scheduled_tick_persist_template.py) 仅用于本 Skill 根目录下 `.persist-fixture-*` 隔离目录的离线合成测试，自动拒绝符号链接与生产目录，不可用于线上监控写入。
