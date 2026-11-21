# Reddit Monitor · Hermes 版

[Skill 入口](skill/SKILL.md) · [当前参考索引](skill/references/INDEX.md) · [运行脚本和测试](runtime/)

`skill/` 是供 Hermes 加载的指令和主题参考；`runtime/` 是配套 Python 代码。内容采集只读取帖子，交给 Agent 判断是否值得处理，本身不发评论。收件箱是独立任务：读取、起草、提交是不同的权限；仓库不附带任何账号的历史授权。

**部署配置**：本地状态命令使用 `REDDIT_MONITOR_DIR`，应指向仓库外的专用状态目录。仅在用户授权和平台条件均已核实后，才为在线采集设置 `REDDIT_MONITOR_AUTHORIZED=1`；还需在部署环境指定 `CAMOFOX_URL`、`CAMOFOX_USER_ID`、独立的 `CAMOFOX_SESSION_KEY` 和 `REDDIT_EXPECTED_USER`。后者也用于核对收件箱回复凭据中的作者。环境变量不代替授权，不要提交实际值。

**离线验证**（Linux、Python 3.10+、Node.js，从仓库根目录执行，不需要账号或部署配置）：

```sh
python3 -m unittest discover -s hermes/runtime -p 'test_reddit*.py'
```

本仓库不提供生产 `state.json`、监控名单、收件箱、评论历史、浏览器会话、密钥或定时任务配置。不要从旧 OpenClaw 目录复制状态，也不要把真实数据写进仓库。离线测试通过不代表已部署或完成线上验证。
