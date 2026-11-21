# Reddit Inbox / Notifications / Chat 分流规程

## 1. 权限与用途核验

- 默认**只读分流**；在已明确的 Reddit Inbox 任务上下文中继续处理无需重复追问范围，但绝不自动衍生长期无人值守回复权限。
- 按本轮实际任务用途区分个人账号自用与商业用途，分别核对读取权限、代发授权与私信联系同意；缺失某项条件时仅跳过受限动作并如实报告未查/未发范围。

## 2. 三通道独立核查标准

每次执行必须分别记录 `old-reddit-messages`、`new-reddit-notifications`、`reddit-chat` 三个来源：

| 状态枚举 | `count` 要求 | 判定准则 |
| :--- | :--- | :--- |
| `verified` | 非负整数（`>= 0`） | 页面真实消息列表已成功渲染并完成核对 |
| `unverified` | 必须为 `null` | 未授权检查、Chat 仅加载前端空壳（无会话列表）、仅看到角标数字未展开或公开 Feed 无法代表私信 |
| `failed` | 必须为 `null` | 网络超时、登录态失效或页面报错（须附带 `coverage` 说明原因） |

> **禁止以偏概全**：单一通道为空不代表全部通道为空；三通道均未验证时不得按“空收件箱成功”结案。

## 3. 消息分类与动作映射

- **`actionable`（需回复）**：包含具体技术追问、参数补充、纠错讨论或尚未解决的实际问题。必须在完整原帖线程中核对目标 `commentId`、父级链路及本账号是否已在后续楼层回复。
- **`handled`（`already_replied`）**：线程回读确认本账号已针对该入站消息作出有效回复，严禁重复发送。
- **`noise`（`no_reply`）**：简短致谢（如 "Thanks!"）、点赞/徽章/成就通知、无新问题的结案反馈。
- **`skippedInsufficientInfo` / `deferred`**：缺少关键上下文或超出本轮处理配额，保留稳定入站 ID 待后续复核。
- **`postedReplies`（`posted_reply`）**：仅在本轮明确授权且完成[发布回读验证](publishing-verification.md)后记录（单轮上限 2 条，且所属通道必须为 `verified`）。

## 4. 状态落库

整理分流证据 JSON 后，调用 [reddit_inbox_state.py](../../runtime/reddit_inbox_state.py) 校验并持久化：

```bash
python3 hermes/runtime/reddit_inbox_state.py status
python3 hermes/runtime/reddit_inbox_state.py persist --result-file inbox_result.json
```
