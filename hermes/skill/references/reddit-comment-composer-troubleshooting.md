# Old Reddit 评论框定位与排障手册

适用于已取得本轮明确发布授权后的 Old Reddit 页面表单操作。严禁因选择器歧义切换账号、更换浏览器会话或绕过前端直接调用未注册 API。

## 1. 排除常见 DOM 陷阱（勿按序号盲选输入框）

Old Reddit 帖子页常同时存在多个相似元素：
- 右侧栏只读短链接框：`input#shortlink-text[readonly]`（对其执行普通输入会触发 `element is not editable`，属元素误选而非浏览器服务崩溃）；
- 隐藏的回复模板克隆表单（`display: none` 或宽高为 `0`）；
- 嵌套楼层中子孙评论各自的 `form.usertext`。

**精确筛选条件**：
1. 顶层评论：`.commentarea form.usertext`，包含 `input[name="thing_id"][value="t3_<post_id>"]`；
2. 嵌套回复：目标 `.thing.comment[data-fullname="t1_<comment_id>"]` 直属回复表单，包含 `input[name="thing_id"][value="t1_<comment_id>"]`，排除其子评论容器 `.child` 内的表单；
3. 目标 `textarea[name="text"]` 必须满足：`!ta.readOnly && !ta.disabled`，且 `ta.getBoundingClientRect().width > 0 && height > 0`，匹配数量精确为 `1`。

## 2. 填入与提交两步分离校验

当快照元素引用存在歧义但 DOM 已唯一锁定目标表单时，可在同一浏览器上下文内分两步执行：

**第一步：仅填入并回读校验（不点击提交）**
```js
ta.focus();
ta.value = finalText;
ta.dispatchEvent(new Event('input', { bubbles: true }));
ta.dispatchEvent(new Event('change', { bubbles: true }));
return {
  parentThingId: form.querySelector('input[name="thing_id"]')?.value || null,
  exactMatch: ta.value === finalText,
  readbackLength: ta.value.length,
};
```

**第二步：核对无误后单次点击专属提交按钮**
- 确认 `exactMatch === true`、`parentThingId` 正确、当前页面不存在本账号同内容已发评论；
- 若表单含 `onsubmit="return post_form(...)"`，确认全局处理函数已加载；
- 精确点击该 `form` 内部的 `button.save[type="submit"]` 一次（切勿使用全页文本匹配 `"save"`，以免误点帖子收藏链接 `a.save-button`）。

## 3. 提交后精确验证

按 [publishing-verification.md](publishing-verification.md) Fresh Navigate 至新评论 Permalink 验证作者、全文、ID 与直接父节点；验证未决时禁止二次点击提交。
