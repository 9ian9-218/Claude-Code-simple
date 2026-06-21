# Teammates 系统升级说明（s16/s17 + CC 对齐）

本次升级参考 `learn-claude-code` 的 **s16_team_protocols**、**s17_autonomous_agents** 教学章节，以及 CC 源码中 `teammateMailbox.ts` / `inProcessRunner.ts` / `utils/tasks.ts` 的设计。修复了此前审查发现的缺陷，并补齐协议状态机与自治任务认领能力。

---

## 新增文件

| 文件 | 实现内容 |
|------|----------|
| `src/teammates/protocol.py` | **s16 协议状态机**：`ProtocolState` 数据类、`register_request` / `get_request` / `match_response` 按 `request_id` 关联请求与响应（含类型校验）；`new_request_id()` 生成唯一 ID；`format_plan_approval_injection()` / `format_idle_notification_injection()` 将结构化消息格式化为 Lead 可读的 user turn。 |
| `src/teammates/inbox_dispatch.py` | **统一 inbox 分发**：`dispatch_inbox_batch()` 将 `task_assignment`、`plan_approval_response`、`permission_response` 等结构化消息转为自然语言注入；`shutdown_request` 走 lifecycle 握手；普通消息包装为 `<teammate-message>` XML；按 index 精确标记已读。 |
| `src/teammates/autonomous.py` | **s17 自治队友**：`idle_poll()` 在 IDLE 阶段每 5s 轮询 inbox + 任务看板，60s 超时退出；对接 `scan_unclaimed_tasks` + `claim_task` 自动认领；`make_identity_block()` / `maybe_reinject_identity()` 在上下文压缩后重注入身份。 |
| `TEAMMATES_UPGRADE.md` | 本说明文档。 |

---

## 修改文件及变更摘要

### 核心生命周期 — `src/teammates/spawn.py`（重写）

- **WORK → IDLE → SHUTDOWN** 三阶段循环（对齐 s17 / CC `inProcessRunner`）。
- `_active_teammates` 改为 `(team_name, name)` 复合键，避免多团队同名冲突。
- 新增 `is_teammate_active(team_name, name)` 供 `list_teammates` 判断 running/offline。
- 退出 `finally` 块调用 `notify_teammate_terminated()`，修复「停止后无法同名重孵化」。
- 使用 `ensure_teammate_for_spawn()` 支持离线成员重新激活。
- 集成 `inbox_dispatch.dispatch_inbox_batch` + `autonomous.idle_poll` 实现空闲自治。
- `request_teammate_shutdown()` 发送 shutdown 时注册 `ProtocolState`（s16 关联追踪）。

### 团队配置 — `src/teammates/team_helpers.py`

- 新增 `find_teammate_member()`、`reactivate_teammate()`、`ensure_teammate_for_spawn()`。
- `add_teammate()` 仅用于首次注册；重孵化走 `ensure_teammate_for_spawn()`。

### Lead 邮箱轮询 — `src/teammates/poller.py`

- 路由 `plan_approval_request` → 注册协议状态 + 注入 Lead 上下文。
- `shutdown_approved` / `shutdown_rejected` → `match_response()` 更新 FSM。
- `teammate_terminated` 增加终端日志。
- `idle_notification` 队列由 `agent_loop._inject_idle_notifications()` 消费。

### 主循环注入 — `src/agent_loop.py`

- 新增 `_inject_idle_notifications()`：将队友空闲通知注入 Lead 对话。
- 注入顺序：权限处理 → 后台通知 → **idle 通知** → teammate 消息。

### 任务系统 — `src/tasks.py`

- 新增 `scan_unclaimed_tasks()`：pending + 无 owner + 依赖已完成的可认领任务（s17）。
- `claim_task()` 增加 **owner 已占用检查**，防止并发覆盖。
- `run_claim_task()` 自动使用当前 agent 名（teammate 认领时 owner=`alice` 而非 `agent`）。

### 权限同步 — `src/permission_sync.py`

- `poll_for_permission_response()` 改为**仅标记匹配的 permission_response 为已读**（按 index），不再误标整个 inbox。

### 工具层 — `src/tool.py`

| 工具 | 角色 | 说明 |
|------|------|------|
| `shutdown_teammate` | Lead | s16 关机协议：发送 `shutdown_request`，追踪 `request_id` |
| `review_plan` | Lead | s16 计划审批：回复 `plan_approval_response` |
| `send_message` (扩展) | Teammate/Lead | 新增 `message_type=plan_approval` 提交计划 |
| `list_tasks` / `claim_task` / `complete_task` / `get_task` | **Teammate** | s17：队友可自主看板、认领、完成任务 |

- `get_all_tools()`：Teammate 工具集 = 基础工具 + `TEAMMATE_ALLOWED_TOOLS`，排除 spawn/create_team 等。
- `create_team`：创建额外团队时**不再抢占** default 团队的 poller 与 `ctx.team_name`。
- `list_teammates`：按 `is_teammate_active(team, name)` 判断 running/offline。

### Prompt — `src/prompt.py`

- `TEAMS_SECTION` 补充：自治认领、关机协议、计划审批、`shutdown_teammate` / `review_plan` 指引。

### 常量 — `src/teammates/constants.py`

- 新增 `TEAMMATE_IDLE_POLL_INTERVAL` (5s)、`TEAMMATE_IDLE_TIMEOUT` (60s)、`TEAMMATE_WORK_MAX_TURNS` (15)、`TEAMMATE_IDENTITY_REINJECT_THRESHOLD` (3)。

### 生命周期 — `src/teammates/lifecycle.py`

- `handle_shutdown_request()` 兼容 `requestId` / `request_id` 字段；增加协议日志。

### 包导出 — `src/teammates/__init__.py`

- 导出 protocol、autonomous、inbox_dispatch、新增 team_helpers API、`is_teammate_active`、`consume_pending_idle_notifications`。

---

## 架构对齐关系

```
                    s16 Team Protocols                    s17 Autonomous Agents
                    ──────────────────                    ─────────────────────
Lead tools          shutdown_teammate, review_plan       create_task (规划)
                    send_message (task_assignment)       spawn_teammate (启动自治队友)

Mailbox types       shutdown_request → shutdown_approved idle_notification → Lead 注入
                    plan_approval_request/response       task_assignment

Teammate lifecycle  协议握手后退出                        WORK → idle_poll → SHUTDOWN
                    notify_teammate_terminated           auto-claim via scan_unclaimed_tasks

CC 源码对应         teammateMailbox.ts (request_id)      inProcessRunner.ts (idle + claim)
                    useInboxPoller.ts (route by type)    utils/tasks.ts (claimTask + can_start)
```

---

## 修复的既有缺陷

1. **同名 teammate 无法二次 spawn** → `ensure_teammate_for_spawn` + 退出时 `notify_teammate_terminated` / `deactivate_teammate`。
2. **`notify_teammate_terminated` 从未调用** → spawn 退出 `finally` 块调用。
3. **权限轮询误标已读** → 按 index 精确标记。
4. **`_active_teammates` 全局名字冲突** → `(team_name, name)` 键。
5. **`idle_notification` 无人消费** → `agent_loop` 注入 Lead。
6. **Lead 无法主动关机** → `shutdown_teammate` 工具。
7. **`create_team` 抢占 poller** → 仅首次绑定 default 团队。
8. **结构化 inbox 注入为原始 JSON** → `inbox_dispatch` 格式化为自然语言。

---

## 使用示例

### Lead：孵化自治队友 + 创建任务看板

```
create_task(subject="实现 API", description="...")
create_task(subject="写测试", description="...", blockedBy=["task_1"])
spawn_teammate(name="alice", role="backend", prompt="你是后端开发者", team_name="", agent_type="general-purpose")
```

队友 idle 后会自动 `scan_unclaimed_tasks` → `claim_task`。

### Lead：计划审批（s16）

Teammate 通过 `send_message(to="team-lead", message_type="plan_approval", message="...")` 提交计划。

Lead 收到 inbox 注入后：

```
review_plan(request_id="plan-xxx", approve=true, feedback="")
```

### Lead：优雅关机（s16）

```
shutdown_teammate(name="alice", team_name="", reason="任务完成")
```

---

## 测试验证

```bash
cd Claude-Code-simple
python -m py_compile main.py src/**/*.py src/teammates/*.py
python -c "from teammates import spawn_teammate, match_response; from tasks import scan_unclaimed_tasks; print('ok')"
```

---

## 尚未实现（CC 完整版差异，可后续迭代）

- 任务文件 `proper-lockfile` 级原子 claim（当前仅有 owner 检查）。
- Plan mode 执行门控（未 approved 时拦截 write/bash）。
- `fs.watch` 任务看板监听（当前为 idle 轮询）。
- Worktree 隔离（s18）。
- 多团队并行 poller（当前 Lead poller 绑定单一 default 团队）。
