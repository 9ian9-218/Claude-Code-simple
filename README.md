# Claude-Code-simple

类 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 架构的 Python Agent 运行时。参考 [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 课程（s01–s20）渐进实现，覆盖 Agent 主循环、工具调用、上下文管理、多 Agent 协作等完整能力栈。

## 特性概览

| 模块 | 课程 | 状态 | 说明 |
|------|------|------|------|
| Agent Loop | s01 | ✅ | ReAct 式主循环，流式 LLM 输出 + Tool Calling |
| Tool Dispatch | s02 | ✅ | `Tool` 抽象 + Schema 校验 + 15+ 内置工具 |
| Todo / Permission | s03 | ✅ | 任务清单、三级权限门控（黑名单 / 规则 / 用户确认） |
| Hooks | s04 | ✅ | 事件驱动扩展（PreToolUse / PostToolUse / Stop） |
| Skill Loading | s05 | ✅ | 扫描 `.claude/skills/` 目录，按需加载 SKILL.md |
| Context Compact | s06 | ✅ | 四层压缩：Snip / Micro / Budget / Auto Compact |
| Task System | s07 | ✅ | 文件持久化任务看板，依赖图 + claim/complete |
| Background Tasks | s13 | ✅ | 长耗时 bash 后台线程，结果注入主循环 |
| Error Recovery | s11 | ✅ | max_tokens 升级、reactive compact、429/529 退避 |
| Memory | s09 | ✅ | Markdown 长期记忆，Stop Hook 异步提取 |
| Subagent | s04 | ✅ | 进程内子 Agent，独立工具权限 |
| Agent Teams | s15 | ✅ | Lead + Teammate，JSONL 邮箱 + 权限同步 |
| Team Protocols | s16 | 🚧 | 结构化 request_id 协议（plan 审批等） |
| Autonomous Agents | s17 | 🚧 | Idle 轮询 + 任务看板自动 claim |
| Worktree Isolation | s18 | ✅ | Git Worktree per-task 隔离 |
| Cron Scheduler | s14 | 🚧 | 持久化定时任务 + 队列处理器 |
| MCP Tools | s19 | 🚧 | 外部 MCP Server 发现与命名空间归一化 |
| Comprehensive Agent | s20 | 🚧 | 全模块统一编排 |

## 架构

```
User Input
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  main.py          入口，初始化 Lead Team + Inbox Poller   │
└──────────────────────────┬──────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────┐
│  agent_loop.py    主循环（最多 100 轮）                   │
│    ├─ inject notifications / teammate inbox              │
│    ├─ compact (L1→L2→L3→L4)                              │
│    ├─ send_messages_with_recovery                        │
│    ├─ PreToolUse hooks → execute → PostToolUse hooks     │
│    └─ Stop hooks (memory extract)                        │
└──────────────────────────┬──────────────────────────────┘
                           ▼
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
   tool.py           teammates/          memory.py
  (15+ tools)     (spawn, mailbox,      compact.py
                   poller, lifecycle)   error_recovery.py
```

### 多 Agent 协作

```
Lead (team-lead)                         Teammate (worker)
     │                                        │
     │  spawn_teammate / send_message         │
     ├───────────────────────────────────────►│ inbox (.mailboxes/*.jsonl)
     │                                        │ agent_loop (独立线程)
     │◄───────────────────────────────────────┤ send_message (progress / result)
     │  Lead Inbox Poller (1s)                │
     │  → inject as user turn                  │
     │                                        │
     │  permission_request ◄──────────────────┤ 危险操作需审批
     │  permission_response ──────────────────►│
```

## 快速开始

### 环境要求

- Python 3.11+
- Git

### 安装

```bash
git clone <your-repo-url>
cd Claude-Code-simple

pip install openai python-dotenv
```

### 配置

在项目根目录创建 `.env`：

```env
OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=https://api.openai.com/v1   # 或兼容 OpenAI 格式的代理地址
OPENAI_MODEL=gpt-4o                          # 或其他支持 Tool Calling 的模型

# 可选
TAVILY_API_KEY=your-tavily-key               # tavily_search 工具
OPENAI_FALLBACK_MODEL=gpt-4o-mini            # 429/529 时的备用模型
```

### 运行

```bash
python main.py
```

交互命令：

| 输入 | 作用 |
|------|------|
| 任意文本 | 发送给 Agent |
| `/new` 或 `/n` | 清空对话历史 |
| `q` / `exit` / 空行 | 退出 |

## 内置工具

| 工具 | 说明 |
|------|------|
| `run_bash` | 执行 shell 命令，支持 `background: true` 后台运行 |
| `read_file` / `write_file` / `edit_file` | 工作区内文件读写与编辑 |
| `glob` | 按模式搜索文件 |
| `todo_write` | 会话内任务清单 |
| `tavily_search` | 网络搜索（需 TAVILY_API_KEY） |
| `load_skill` | 加载 `.claude/skills/` 下的 SKILL.md |
| `subagent_task` | 启动进程内子 Agent |
| `create_task` / `list_tasks` / `get_task` | 持久化任务看板 |
| `claim_task` / `complete_task` | 任务认领与完成 |
| `create_team` / `spawn_teammate` | 创建团队、孵化 Teammate |
| `send_message` / `list_teammates` | 团队消息与成员查询 |

Teammate 和 Subagent 的工具集自动受限（不可 spawn 嵌套 Teammate 等）。

## Worktree 隔离

当 Agent **claim** 一个 task 时，自动在 `.claude/worktrees/<task_id>/` 创建 git worktree，
并切换到该 worktree 的分支 `claude/task-<task_id>`。
之后所有文件读写、bash 操作都局限在 worktree 内，与原工作区隔离。

- **claim** → 创建 worktree + 切换工作目录
- **complete** → 删除 worktree + 恢复原工作目录
- 如果 git 不可用或工作区不干净，worktree 创建静默跳过，不影响 task 流程
- worktree 目录已被 `.gitignore` 排除，不会提交到仓库

### 使用示例

```bash
# Agent 创建并认领任务后自动进入 worktree
User >  create_task subject="重构 auth 模块"
# → Created task_1: 重构 auth 模块
User >  claim_task task_id="task_1"
# → [worktree] created at .claude/worktrees/task_1/ (branch: claude/task-task_1)
# → [worktree] switched to .../.claude/worktrees/task_1
# → [claim] 重构 auth 模块 → in_progress (owner: agent)

# 此时所有文件操作都在 worktree 内进行，不影响主仓库
User >  complete_task task_id="task_1"
# → [worktree] removing worktree task_1
# → [complete] 重构 auth 模块 ✓
# → 回到主仓库工作目录
```

## 项目结构

```
Claude-Code-simple/
├── main.py                 # 入口
├── src/
│   ├── agent_loop.py       # Agent 主循环
│   ├── client.py           # OpenAI 客户端 + 流式输出
│   ├── tool.py             # Tool 抽象与全部内置工具
│   ├── hook.py             # Hook 注册表
│   ├── prompt.py           # System Prompt 组装
│   ├── compact.py          # 四层上下文压缩
│   ├── memory.py           # 长期记忆
│   ├── tasks.py            # 任务看板
│   ├── worktree.py         # Git Worktree 隔离（s18）
│   ├── background_task.py  # 后台任务线程
│   ├── messageQueueManager.py  # 异步通知队列
│   ├── error_recovery.py   # LLM 错误恢复
│   ├── check_permissions.py    # 权限规则
│   ├── permission_sync.py  # 跨 Agent 权限同步
│   ├── skill_load.py       # Skill 扫描与加载
│   ├── config.py           # 运行时配置
│   └── teammates/          # 多 Agent 协作
│       ├── spawn.py        # Teammate 孵化与独立 Loop
│       ├── mailbox.py      # JSONL 消息总线
│       ├── poller.py       # Lead Inbox 轮询
│       ├── lifecycle.py    # 生命周期（idle / shutdown）
│       ├── message_types.py    # 结构化协议消息
│       └── team_helpers.py # 团队配置持久化
├── .claude/                # 项目内运行时数据
│   ├── teams/              # 多 Agent 团队配置与邮箱
│   ├── memory/             # 长期记忆
│   ├── tasks/              # 任务看板
│   ├── skills/             # 可加载的 Skill 定义
│   └── worktrees/          # 任务隔离 Git Worktree（自动管理）
└── .env                    # 环境变量（不提交）
```

团队、记忆、任务、Skill 均持久化在项目根目录的 `.claude/` 下，不写入 `~/.claude`。

## 核心机制

### Hook 事件

| 事件 | 触发时机 | 内置 Hook |
|------|----------|-----------|
| `UserPromptSubmit` | 用户输入 | 工作目录提示 |
| `PreToolUse` | 工具执行前 | Schema 校验、权限检查、日志 |
| `PostToolUse` | 工具执行后 | 大输出告警 |
| `Stop` | 模型自然结束 | 会话统计、记忆提取 |

通过 `register_hook(event, callback)` 扩展，无需修改主循环。

### 四层上下文压缩

| 层级 | 策略 | 触发条件 |
|------|------|----------|
| L3 Budget | 超大 Tool Result 落盘，保留预览 | 每轮循环 |
| L1 Snip | 裁剪中间消息 | 消息数 > 240 |
| L2 Micro | 旧 Tool Result 替换为占位符 | 每轮循环 |
| L4 Auto Compact | LLM 摘要整段历史 | Token > 480K |

### 错误恢复

- **max_tokens**：finish_reason=length 时升级至 64K 并续写
- **prompt_too_long**：reactive compact 后重试（一次）
- **429 / 529**：指数退避 + jitter，连续失败后切换备用模型

## 使用示例

**单 Agent 编程任务**

```
User >  读取 src/agent_loop.py，解释主循环的执行流程
```

**后台长任务**

```
User >  在后台扫描项目中所有 TODO 注释，完成后告诉我结果
```

Agent 会调用 `run_bash(background=true)`，完成后通过 `<task_notification>` 注入结果。

**多 Agent 分工**

```
User >  创建一个 test-runner 队友负责跑测试，你自己审查代码结构
```

Lead 会 `spawn_teammate` 并通过 `send_message` 分配任务，Teammate 在独立线程中运行 Agent Loop，结果回传 Lead 邮箱。

## 扩展 Skill

在 `.claude/skills/<name>/SKILL.md` 中添加 Frontmatter 定义：

```markdown
---
name: my-skill
description: 简要描述，供 Agent 决定是否加载
---

# Skill 正文
...
```

Agent 通过 `load_skill(name="my-skill")` 按需加载。



## 参考

- [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) — 逐课 Agent 架构教程（s01–s20）
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — 原版产品设计参考

## License

MIT
