"""
tool.py — Claude Code 风格的工具管理模块

本文件模仿 Claude Code 的工具架构：由 build_tool() 创建为一个独立的 Tool 对象，把 schema、验证、权限、执行封装在一起。

文件结构概览
────────────
  1. 类型别名 & 全局配置
  2. Tool 数据类          — 工具的核心抽象，含 run() 执行管线
  3. build_tool()         — 工厂函数，创建 Tool 实例
  4. 通用校验工具          — validate_args()，由 hook.py 的 validate_hook 调用
  5. 各工具的执行函数       — _exec_* 前缀，纯业务逻辑
  6. 各工具的 JSON Schema  — _*_SCHEMA 常量
  7. 各工具的 Tool 实例     — *_TOOL 常量，由 build_tool() 组装
  8. 对外 API              — get_all_base_tools / get_openai_tools / execute_tool_call

一次工具调用的完整流程（对应 Tool.run()）
────────────────────────────────────────
  LLM 返回 tool_call
       │
       ▼
  execute_tool_call()          ← agent_loop.py 调用入口
       │  解析 JSON 参数
       ▼
  Tool.run(args) → execute(args)
       │
       ▼
  （validate / permission 均在 agent_loop 的 PreToolUse hook 中完成）
       │
       ▼
  结果序列化为 JSON 字符串，追加到 messages 发回 LLM
"""

import glob as glob_module
import json
import os
import subprocess
import urllib.request
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable
from skill_load import SKILL_REGISTRY
# Agent 的工作目录根路径，所有文件操作都限制在此目录内
ExecuteFn = Callable[[dict[str, Any]], Any]


# ── Tool 核心抽象 ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Tool:
    """
    工具的完整描述，对应 Claude Code 中 buildTool() 返回的对象。
    frozen=True 表示实例创建后不可修改，保证工具定义在运行期是常量。
    字段说明：
        name              — 工具名，LLM 通过此名称发起调用
        description       — 工具描述，告诉 LLM 何时该用这个工具
        parameters        — JSON Schema，描述参数结构（传给 OpenAI API，也用于统一校验）
        execute           — 执行函数（真正的副作用在这里发生）
        is_read_only      — 是否只读，供上层并发调度参考（本文件暂未使用）
    """
    name: str
    description: str
    parameters: dict[str, Any]
    execute: ExecuteFn
    is_read_only: bool = False

    def to_openai_schema(self) -> dict[str, Any]:
        """
        将 Tool 转换为 OpenAI Chat Completions API 要求的 tools 格式。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "strict": True,
                "parameters": self.parameters,
            },
        }

    def run(self, args: dict[str, Any]) -> Any:
        """执行工具逻辑。参数校验由 PreToolUse 的 validate_hook 负责。"""
        return self.execute(args)


# ── build 函数, 构建工具 ──────────────────────────────────────────────────────────────
def build_tool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any],
    execute: ExecuteFn,
    is_read_only: bool = False,
) -> Tool:
    """创建 Tool 实例。参数校验由 PreToolUse validate_hook 负责。"""
    return Tool(
        name=name,
        description=description,
        parameters=parameters,
        execute=execute,
        is_read_only=is_read_only,
    )

from config import WORKDIR
# ── 路径校验工具 ──────────────────────────────────────────────────────────
def _check_path(p: str) -> str | None:
    """检查路径是否在 WORKDIR 内，返回错误信息或 None。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        return f"Path escapes workspace: {p}"
    return None

def safe_path(p: str) -> Path:
    """解析并返回工作区内的安全路径，供 execute 阶段使用。"""
    err = _check_path(p)
    if err:
        raise ValueError(err)
    return (WORKDIR / p).resolve()

# ── 各工具的 execute 函数、JSON Schema 定义及 Tool 实例 ─────────────────────────────────────────────

#tavily_search
def _exec_tavily_search(args: dict[str, Any]) -> dict[str, Any]:
    """调用 Tavily Search API 进行网络搜索，返回摘要结果。"""
    query = args["query"]
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return {"status": "error", "message": "TAVILY_API_KEY not set"}

    # 构造 POST 请求体
    payload = json.dumps({
        "query": query,
        "search_depth": "basic",
        "max_results": 3,
        "include_answer": True,
        "include_raw_content": False,
        "api_key": api_key,
    }).encode()

    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return {"status": "error", "message": str(e)}

    # 组装可读的结果文本
    parts = []
    if data.get("answer"):
        parts.append(f"Answer: {data['answer']}")
    for i, item in enumerate(data.get("results", [])[:3], 1):
        parts.append(
            f"{i}. {item.get('title', '')}\n"
            f"   URL: {item.get('url', '')}\n"
            f"   {item.get('content', '')}"
        )
    return {
        "status": "success",
        "message": "\n\n".join(parts) if parts else "No results found.",
    }
_TAVILY_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "The search query"},
    },
    "required": ["query"],
    "additionalProperties": False,
}
TAVILY_SEARCH_TOOL = build_tool(
    name="tavily_search",
    description=(
        "Search the web for up-to-date information using Tavily. "
        "Use when the user asks about recent events, facts, or anything that may require web search."
    ),
    parameters=_TAVILY_SCHEMA,
    execute=_exec_tavily_search,
    is_read_only=True,
)

#run_bash
def _exec_run_bash(args: dict[str, Any]) -> str:
    """在 shell 中执行命令，捕获 stdout+stderr，最长等待 120 秒。"""
    command = args["command"]
    try:
        r = subprocess.run(
            command, shell=True, cwd=os.getcwd(),
            capture_output=True, text=True, timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"  # 截断过长输出
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

_BASH_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "The command to run"},
    },
    "required": ["command"],
    "additionalProperties": False,
}
RUN_BASH_TOOL = build_tool(
    name="run_bash",
    description="Run a shell command. Use when the user asks to run a command.",
    parameters=_BASH_SCHEMA,
    execute=_exec_run_bash,
    is_read_only=False,
)

#read_file
def _exec_read_file(args: dict[str, Any]) -> str:
    """读取文件内容，可选 limit 参数限制返回行数。"""
    path = args["path"]
    limit = args.get("limit")
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit:
            lines = lines[:limit]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The path of the file to read"},
        #"limit": {"type": "integer", "description": "Max number of lines to read"},
    },
    "required": ["path"],
    "additionalProperties": False,
}
READ_FILE_TOOL = build_tool(
    name="read_file",
    description="Read file contents at a specific path.",
    parameters=_READ_SCHEMA,
    execute=_exec_read_file,
    is_read_only=True,
)

#write_file
def _maybe_rebuild_memory_index(file_path: Path) -> None:
    """write_file 写入 .memory/*.md 后刷新 MEMORY.md 索引（MEMORY.md 本身除外）。"""
    from memory import MEMORY_DIR, _rebuild_index

    if file_path.name == "MEMORY.md" or file_path.suffix != ".md":
        return
    if file_path.parent == MEMORY_DIR.resolve():
        _rebuild_index()


def _exec_write_file(args: dict[str, Any]) -> str:
    """将 content 写入指定路径（覆盖已有内容）。"""
    path = args["path"]
    content = args["content"]
    try:
        file_path = safe_path(path)
        file_path.write_text(content)
        _maybe_rebuild_memory_index(file_path)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The path of the file to write"},
        "content": {"type": "string", "description": "The content to write into the file"},
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}
WRITE_FILE_TOOL = build_tool(
    name="write_file",
    description="Write content to a file at a specific path.",
    parameters=_WRITE_SCHEMA,
    execute=_exec_write_file,
    is_read_only=False,
)

#edit_file
def _exec_edit_file(args: dict[str, Any]) -> str:
    """在文件中精确替换一次 old_text → new_text（类似 search-and-replace）。"""
    path = args["path"]
    old_text = args["old_text"]
    new_text = args["new_text"]
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text:
            return "Error: text not found"
        file_path.write_text(text.replace(old_text, new_text, 1))  # count=1 只替换第一处
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

_EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The path of the file to edit"},
        "old_text": {"type": "string", "description": "Exact text to replace"},
        "new_text": {"type": "string", "description": "Replacement text"},
    },
    "required": ["path", "old_text", "new_text"],
    "additionalProperties": False,
}
EDIT_FILE_TOOL = build_tool(
    name="edit_file",
    description="Replace exact text in a file once.",
    parameters=_EDIT_SCHEMA,
    execute=_exec_edit_file,
    is_read_only=False,
)

#glob
def _exec_glob(args: dict[str, Any]) -> str:
    """按 glob 模式匹配 WORKDIR 下的文件，返回换行分隔的路径列表。"""
    pattern = args["pattern"]
    try:
        recursive = "**" in pattern
        return "\n".join(glob_module.glob(pattern, root_dir=WORKDIR, recursive=recursive))
    except Exception as e:
        return f"Error: {e}"

_GLOB_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Glob pattern relative to WORKDIR (e.g. '**/*.py', 'Claude-Code/skills/*'). Match case exactly — Linux is case-sensitive.",
        },
    },
    "required": ["pattern"],
    "additionalProperties": False,
}
GLOB_TOOL = build_tool(
    name="glob",
    description="Match and list files using a glob pattern. Paths are relative to WORKDIR; match case exactly (Linux is case-sensitive).",
    parameters=_GLOB_SCHEMA,
    execute=_exec_glob,
    is_read_only=True,
)

#todo_list_write
CURRENT_TODOS: list[dict] = []
def _exec_todo_write(args: dict[str, Any]) -> str:
    global CURRENT_TODOS
    todos = args["todos"]
    # validate required fields
    for i, t in enumerate(todos):
        if "content" not in t or "status" not in t:
            return f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return f"Error: todos[{i}] has invalid status '{t['status']}'"
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"

_TODO_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "description": "Task list for the current coding session",
            "items": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Task description",
                    },
                    "status": {
                        "type": "string",
                        "description": "Task status",
                        "enum": ["pending", "in_progress", "completed"],
                    },
                },
                "required": ["content", "status"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["todos"],
    "additionalProperties": False,
}
TODO_WRITE_TOOL = build_tool(
    name="todo_write",
    description=(
        "Session checklist for micro-steps within the *current* persisted task only. "
        "For large multi-step goals, plan first with create_task/list_tasks, not todo_write alone."
    ),
    parameters=_TODO_WRITE_SCHEMA,
    execute=_exec_todo_write,
    is_read_only=True,
)

#subagent_task
from prompt import SUBAGENT_IDENTITY

def _spawn_subagent(args: dict[str, Any]) -> str:
    """子 Agent：独立 messages[]，仅返回最终文本摘要。"""
    from agent_loop import agent_loop

    description = args["description"]
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    messages = [
        {"role": "system", "content": SUBAGENT_IDENTITY},
        {"role": "user", "content": description},
    ]
    result = agent_loop(messages, max_turn=30, max_tokens=6000, isSubagent=True)
    if result:
        print("\033[35m[Subagent done]\033[0m")
        return result
    return "Subagent stopped after 30 turns without final answer."

_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": "What the subagent should accomplish",
        },
    },
    "required": ["description"],
    "additionalProperties": False,
}
SUBAGENT_TASK_TOOL = build_tool(
    name="subagent_task",
    description=(
        "Launch a subagent for an isolated sub-problem. "
        "Not for the main plan-and-resolve workflow (use create_task/claim_task there)."
    ),
    parameters=_TASK_SCHEMA,
    execute=_spawn_subagent,
    is_read_only=False,
)

def _load_skill(args: dict[str, Any]) -> str:
    name = args["name"]
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]
_LOAD_SKILL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The name of the skill to load"},
    },
    "required": ["name"],
    "additionalProperties": False,
}
LOAD_SKILL_TOOL = build_tool(
    name="load_skill",
    description="Load the full content of a skill by name.",
    parameters=_LOAD_SKILL_SCHEMA,
    execute=_load_skill,
    is_read_only=True,
)

# ── Task system（.tasks/ 持久化）────────────────────────────────────

from tasks import (
    run_claim_task,
    run_complete_task,
    run_create_task,
    run_get_task,
    run_list_tasks,
)

def _exec_create_task(args: dict[str, Any]) -> str:
    blocked = args["blockedBy"]
    return run_create_task(
        args["subject"],
        args["description"],
        blocked if blocked else None,
    )


def _exec_list_tasks(args: dict[str, Any]) -> str:
    return run_list_tasks(args["status_filter"])


def _exec_get_task(args: dict[str, Any]) -> str:
    return run_get_task(args["task_id"])


def _exec_claim_task(args: dict[str, Any]) -> str:
    return run_claim_task(args["task_id"])


def _exec_complete_task(args: dict[str, Any]) -> str:
    return run_complete_task(args["task_id"])


_CREATE_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string", "description": "Short task title"},
        "description": {
            "type": "string",
            "description": "Detailed description; use empty string if none",
        },
        "blockedBy": {
            "type": "array",
            "description": "Dependency task IDs; use empty array if none",
            "items": {"type": "string"},
        },
    },
    "required": ["subject", "description", "blockedBy"],
    "additionalProperties": False,
}
CREATE_TASK_TOOL = build_tool(
    name="create_task",
    description=(
        "Plan phase: create a persisted task in .tasks/ (use during initial planning "
        "for large multi-step goals). Set blockedBy for dependencies. "
        "Pass empty string / empty array when description or blockedBy are not needed. "
        "Create the full plan before claim_task or implementation tools."
    ),
    parameters=_CREATE_TASK_SCHEMA,
    execute=_exec_create_task,
    is_read_only=False,
)

_LIST_TASKS_SCHEMA = {
    "type": "object",
    "properties": {
        "status_filter": {
            "type": "string",
            "enum": ["all", "pending", "in_progress", "completed"],
            "description": "Filter by status, or 'all' for every task",
        },
    },
    "required": ["status_filter"],
    "additionalProperties": False,
}
LIST_TASKS_TOOL = build_tool(
    name="list_tasks",
    description=(
        "Plan phase: list persisted tasks after create_task to verify the plan. "
        "Use status_filter='all' before starting execution."
    ),
    parameters=_LIST_TASKS_SCHEMA,
    execute=_exec_list_tasks,
    is_read_only=True,
)

_GET_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "Task ID, e.g. task_1"},
    },
    "required": ["task_id"],
    "additionalProperties": False,
}
GET_TASK_TOOL = build_tool(
    name="get_task",
    description="Get full details of a specific task by ID.",
    parameters=_GET_TASK_SCHEMA,
    execute=_exec_get_task,
    is_read_only=True,
)

_CLAIM_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "Task ID to claim"},
    },
    "required": ["task_id"],
    "additionalProperties": False,
}
CLAIM_TASK_TOOL = build_tool(
    name="claim_task",
    description=(
        "Resolve phase: claim ONE pending task (dependencies must be completed) "
        "before doing implementation work. Sets status to in_progress."
    ),
    parameters=_CLAIM_TASK_SCHEMA,
    execute=_exec_claim_task,
    is_read_only=False,
)

_COMPLETE_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "Task ID to complete"},
    },
    "required": ["task_id"],
    "additionalProperties": False,
}
COMPLETE_TASK_TOOL = build_tool(
    name="complete_task",
    description=(
        "Resolve phase: mark the claimed in_progress task completed after its work "
        "is done. Reports unblocked downstream tasks; then claim the next one."
    ),
    parameters=_COMPLETE_TASK_SCHEMA,
    execute=_exec_complete_task,
    is_read_only=False,
)

# ── 工具列表 ──────────────────────────────────────────────────────────────
TOOLS = [
    TAVILY_SEARCH_TOOL,
    RUN_BASH_TOOL,
    READ_FILE_TOOL,
    WRITE_FILE_TOOL,
    EDIT_FILE_TOOL,
    GLOB_TOOL,
    TODO_WRITE_TOOL,
    CREATE_TASK_TOOL,
    LIST_TASKS_TOOL,
    GET_TASK_TOOL,
    CLAIM_TASK_TOOL,
    COMPLETE_TASK_TOOL,
    SUBAGENT_TASK_TOOL,
    LOAD_SKILL_TOOL,
]

# 工具名 → Tool 实例 的查找表
TOOL_MAP = {tool.name: tool for tool in TOOLS}

def _get_tool_map() -> dict[str, Tool]:
    return TOOL_MAP

# ── 对外 API ──────────────────────────────────────────────────────────────

SUBAGENT_EXCLUDED_TOOLS = frozenset({
    "todo_write",
    "subagent_task",
    "tavily_search",
    "create_task",
    "claim_task",
    "complete_task",
})  # 子 agent 不可见的工具
def get_all_tools(isSubagent=False) -> list[dict[str, Any]]:
    """
    返回所有工具的 OpenAI schema 列表。
    client.py 调用此函数获取 tools 参数，传给 chat.completions.create()。
    子 agent 的工具限制在此处统一配置即可，模型看不到的工具不会被请求。
    """
    if isSubagent:
        return [tool.to_openai_schema() for tool in TOOLS if tool.name not in SUBAGENT_EXCLUDED_TOOLS]
    return [tool.to_openai_schema() for tool in TOOLS]

def execute_tool_call(tool_call, args: dict[str, Any] | None = None) -> str:
    """
    执行 LLM 返回的单次 tool_call。

    PreToolUse / PostToolUse hook 由 agent_loop.py 在调用前后触发。
    若 agent_loop 已解析参数，可传入 args 避免重复解析。
    """
    name = tool_call.function.name
    tool = TOOL_MAP.get(name)
    if tool is None:
        return json.dumps({"status": "error", "message": f"Unknown tool: {name}"})

    if args is None:
        try:
            args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError as e:
            return json.dumps({"status": "error", "message": f"Invalid arguments JSON: {e}"})

        if not isinstance(args, dict):
            return json.dumps({"status": "error", "message": "Arguments must be a JSON object"})

    result = tool.run(args)
    # bash/file 工具返回 str，tavily 返回 dict，统一处理
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)
