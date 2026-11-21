# 当前发布与嵌套验证

## 权限
固定稿授权与委托选帖/起草发布授权按主 SKILL 区分；固定稿实质变化需确认，明确委托范围内确定最终文本无需重复逐字批准。范围或风险变化仅暂停相关项。不得从历史成功发布推导本轮权限。

## 提交前
1. 使用获批配置指定的同一 Camofox `userId`（文档不提供 ID），核对真实登录账户、精确 permalink、实时正文、评论及板规（含 `/about/rules` 与侧栏）、推广、链接与适用自动化限制；分别核对用户授权、登录身份和平台条件。
2. 涉及利益相关推荐如实披露关联，不虚构使用经历。核实技术规格与安全性；不建议带压拆接或强接。
3. 顶层：唯一可见、可编辑 textarea[name="text"]，其表单隐藏 thing_id=t3_<post_id>。
4. 嵌套：目标 .thing.comment[data-fullname="t1_<comment_id>"] 的专属回复框，隐藏 thing_id=t1_<comment_id>。排除后代其他评论的表单；仅 DOM contains 可能包含更深后代，不能独立证明直接 Parent。
5. 排除 shortlink readonly、disabled 和隐藏克隆。回读 Composer 全文并与批准固定稿/本轮委托最终稿逐字核对，再提交一次。ref 或填入结果有歧义时，按 [评论框操作流程](reddit-comment-composer-troubleshooting.md) 在同一页面确认唯一表单，分开执行填入回读与点击提交，不改用其他身份或发布 API。

## 提交后
fresh reload，核对作者、全文、Comment ID、精确 Permalink 和直接 Parent。按钮点击、计数上升、片段匹配、页面残留文本均非单独成功证据。
结果不明先查，不盲重发；无人值守发送上线前必须验证提交前的持久化防重闸门，见[状态契约](state-persistence.md)。编辑框还留着文字也不证明未发。确认未提交后仅在原授权范围内有界重试。错楼层/重复内容先报告，删除必须已有明确目标授权并回读，不把“修复”当自动删除权。
发布核验后按 [state-persistence.md](state-persistence.md) 先保留独立外部回执，再更新本地去重状态；本地记录失败不授权重发。仅清理本任务创建、不共享的临时标签；保留其他任务及用户标签，不清空 Profile 会话。
