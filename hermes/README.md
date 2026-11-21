# Reddit Monitor · Hermes 部署与更新指南（v2.2.0 优化版）

本目录是专为 **Hermes Agent + Camofox** 环境打包的直接替换升级包（与上游仓库 `hermes/` 目录结构完全对齐）：

- `skill/`：供 Hermes 加载的指令、参考手册与隔离测试模板（目标位置：`~/.hermes/skills/reddit-monitor/` 或对应 Profile 下的 `skills/reddit-monitor/`）。
- `runtime/`：供 Cron 定时采集、语义复核落库、归档与 Inbox 分流调用的 Python 运行时与全套离线测试（注意：**升级后新增了 `file_lock.py`，必须与 `reddit_monitor_tick_noagent.py`、`reddit_inbox_state.py` 放在同一目录**）。
- `deploy_to_hermes.py`：一键安全升级脚本（支持 `--dry-run`、自动时间戳备份、自动跳过生产 `state.json` 与 Cron 配置、升级后自动跑离线回归测试）。

---

## 一、一键安全更新（推荐）

将 `hermes/` 目录同步到运行 Hermes 的机器（如 Linux 服务器、WSL 或 macOS）后执行：

```bash
# 1. 先执行演练模式（不修改任何文件，查看将更新的目标路径）
python3 hermes/deploy_to_hermes.py \
  --hermes-home ~/.hermes \
  --runtime-dir /path/to/your/reddit-monitor-runtime \
  --dry-run

# 2. 确认路径无误后正式执行更新（自动备份旧版并运行 85 项离线验证）
python3 hermes/deploy_to_hermes.py \
  --hermes-home ~/.hermes \
  --runtime-dir /path/to/your/reddit-monitor-runtime
```

> **提示**：如果您在 Hermes 中使用了多 Profile（如 `~/.hermes/profiles/<profile_name>/skills/`），传入 `--skill-dir ~/.hermes/profiles/<profile_name>/skills/reddit-monitor` 即可精确指定。若您此前并未单独部署 `runtime/`，只需省略 `--runtime-dir` 先仅更新 Skill。

---

## 二、手动替换步骤（若不使用脚本）

1. **备份现有目录**：
   ```bash
   cp -a ~/.hermes/skills/reddit-monitor ~/.hermes/skills/reddit-monitor.bak.$(date +%Y%m%d%H%M%S)
   ```
2. **更新 Skill 目录**：
   用本目录下的 `hermes/skill/` 完整替换 `~/.hermes/skills/reddit-monitor/`（包含 `SKILL.md`、`references/*.md`、`scripts/scheduled_tick_persist_template.py`）。
3. **更新 Runtime 脚本（关键：务必包含 `file_lock.py`）**：
   将以下 3 个文件复制到您线上 Cron 实际调用的脚本目录（可通过检查 `~/.hermes/cron/jobs.json` 确认实际脚本路径）：
   - `hermes/runtime/file_lock.py` **（v2.2 新增公共锁与原子写模块，必传）**
   - `hermes/runtime/reddit_monitor_tick_noagent.py`
   - `hermes/runtime/reddit_inbox_state.py`
   > **切勿触碰生产状态目录**：不要覆盖 `REDDIT_MONITOR_DIR` 下的 `state.json`、`review-cache/`、`inbox-cache/`，新版数据结构对旧版 `state.json` **100% 向后兼容**。
4. **验证生效**：
   ```bash
   # 运行全套 85 项离线回归测试（无需联网或账号）
   python3 -m unittest discover -s hermes/runtime -p 'test_*.py' -v

   # 检查 Hermes 已识别更新后的 Skill
   hermes skills list
   ```

---

## 三、升级后解锁的新能力

1. **自定义任意社区领域词（摆脱高压水枪硬编码）**：可在生产 `REDDIT_MONITOR_DIR/state.json` 的每个 `watch` 对象中可选添加 `"domainTerms": ["词1", "词2"]`（未添加时自动保持原默认行为，零破坏兼容）。
2. **消除子串误报**：`word_hit` 现已对全部字母数字关键词强制边界匹配（不再因 `psi` 误匹配 `upside`/`pepsi` 或因 `pump` 误匹配 `pumpkin`）。
3. **安全归档 CLI**：支持 `python3 reddit_monitor_tick_noagent.py archive --archive-file req.json` 安全清理超期 `manual_pending` 并自动生成 `archive-journal` 不可变备份。
4. **更严密的一致性防线**：修复了正文阶段跨帖去重误计为 `failed_agent`、`summary` 漏检候选删改/证据漂移，以及 Inbox `A→B→A` 旧批重放回拨水位的问题。
