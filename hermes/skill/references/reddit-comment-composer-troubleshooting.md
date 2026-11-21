# 评论框定位与提交验证

适用于已获得本轮发布授权、固定 Camofox 会话中的 Old Reddit 评论框操作。权限和回读标准以主 SKILL 及 [publishing-verification.md](publishing-verification.md) 为准；不要切换身份、浏览器后端或直接调用 Reddit 发布 API。

## 1. 先确认目标，不靠 ref 序号猜输入框

- 用当前页面 DOM 确认登录账号、帖子 fullname 和现有本账号评论，避免回复到错误帖子或重复提交。
- 顶层表单从 `.commentarea form.usertext` 选择，要求包含 `textarea[name="text"]`，且 `input[name="thing_id"]` 等于目标 `t3_<post_id>`。嵌套回复使用目标评论的专属表单和 `t1_<comment_id>`，排除后代评论表单。
- 要求匹配唯一，textarea 非 readonly/disabled，且 `getBoundingClientRect()` 宽高均大于0；排除 shortlink 输入框和隐藏克隆。
- 普通输入调用失败后，先回读 textarea 当前值、可编辑状态及已发表评论。输入失败本身不是提交失败或服务失效的证据；不要立即重发或切换登录态。

## 2. 在同一页面安全填入并回读

优先用最新快照中已确认的输入元素。若 ref 无法可靠定位，但 DOM 已唯一确认表单，可通过同一 Camofox 的 `browser_console` 在该 textarea 内执行：

```js
ta.focus();
ta.value = finalText;
ta.dispatchEvent(new Event('input', { bubbles: true }));
ta.dispatchEvent(new Event('change', { bubbles: true }));
```

仅填入本轮确定的最终文本；此步不点击提交。返回完整 `ta.value`、隐藏 Parent 和 `ta.value === finalText`，让填入和提交形成两个独立检查点。Old Reddit 表单若使用 `onsubmit="return post_form(...)"`，确认对应页面处理函数存在；函数缺失时不能把按钮点击当作成功。

## 3. 只提交一次

提交调用中再次检查账号、唯一目标表单、Parent、全文逐字一致、可用的 `button.save[type="submit"]`，并确认没有本账号的同内容评论，然后仅点击该表单按钮一次。不要用页面级“save”文本选择器，因为保存帖子/评论链接也可能同名。

## 4. 通过精确评论链接验证

- 提交后先读取新评论的作者、全文、Comment ID 和 permalink；这些信息用于找到验证目标，不单独构成完成。
- Fresh navigate 到返回的精确评论 permalink，再核对作者、完整正文和所属帖子。顶层评论应没有祖先 `.thing.comment`；嵌套评论必须确认直接 Parent，不能仅用任意祖先 contains 证明。
- 编辑框仍有文字、隐藏编辑副本或过期的“暂无评论”提示均不证明未发送；先核对精确链接，避免二次提交造成重复。
- 没有可验证评论时，检查可见错误/限流和当前页面状态；结果未知则停止提交并报告，不盲重试。只有确认未发送后才能在原授权内有界重试。

## 5. 分开处理发布与本地记录

发布经 fresh reload 验证后，按 [state-persistence.md](state-persistence.md) 保存独立回执并更新去重状态。本地落库失败不把真实评论降格为“未发”，也不重新发布；只报告本地记录未完成。最后仅关闭本任务创建的临时标签。
