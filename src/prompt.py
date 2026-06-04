"""
prompt.py — 项目内所有 prompt 模板的统一入口。

组装规则（主 agent system）:
  AGENT_IDENTITY + skill_catalog + memory_section
  由 assemble_system_prompt(context) 按 context 动态拼接；
  get_system_prompt(context) 在 context 未变时返回缓存，避免重复拼接。

子 agent:
  SUBAGENT_IDENTITY + skill_catalog（无 memory 段）

运行时刷新:
  update_context(context, messages) 从磁盘/工具注册表收集最新状态，
  供 get_system_prompt 决定是否重新组装。
"""
"""
CC 的 system prompt 的 section 相关参数如下：
数量不固定，受 feature flag、output style、KAIROS/Proactive 模式、用户类型、token 预算等影响。大致分两类：

静态 section（始终加载）：identity、system、doing_tasks、actions、using_tools、tone_style、output_efficiency 等。

动态 section（按状态加载）：session_guidance、memory、ant_model_override、env_info_simple、language、output_style、mcp_instructions、scratchpad、frc、summarize_tool_results、numeric_length_anchors、token_budget、brief 等。

mcp_instructions 是唯一的易失性 section（通过 DANGEROUS_uncachedSystemPromptSection() 创建），因为 MCP server 可以在轮次间连接和断开。
"""

import json

from config import WORKDIR

# =============================================================================
# System prompt — 主 agent
# =============================================================================

AGENT_IDENTITY = (
    f"You are a coding agent at {WORKDIR}. "
    "For isolated deep dives, use subagent_task. "
    "For the current turn's short checklist, use todo_write."
)

# 大型多步目标：先规划（create_task）再执行（claim → work → complete）
TASK_PLANNING_SECTION = (
    f"\n\n## Plan and resolve (persisted tasks in {WORKDIR}/.tasks)\n"
    "When the user gives a large or multi-step goal (feature, refactor, migration, "
    "several files, or work that may span many tool rounds), use the persisted task "
    "system — do NOT jump straight into bash/read/write for the whole goal.\n\n"
    "**Phase 1 — Plan (before implementation tools):**\n"
    "1. Break the goal into ordered steps with clear subjects.\n"
    "2. Call create_task for each step; use blockedBy for dependencies "
    "(e.g. tests blockedBy API task id).\n"
    "3. Call list_tasks with status_filter='all' to confirm the plan.\n"
    "4. Optionally use todo_write for the *current* step's micro-actions only.\n\n"
    "**Phase 2 — Resolve (one persisted task at a time):**\n"
    "1. claim_task on the next pending task whose dependencies are satisfied.\n"
    "2. Do the work with read_file, write_file, run_bash, etc.\n"
    "3. complete_task when that step is done; check which tasks were unblocked.\n"
    "4. Repeat until all tasks are completed or the user stops you.\n\n"
    "Rules:\n"
    "- Do not claim multiple persisted tasks in parallel.\n"
    "- Do not complete_task without having claimed it first.\n"
    "- Add new create_task entries only if the plan truly changes; prefer finishing "
    "the existing plan first.\n"
    "- Simple one-shot requests (read one file, run one command) do not need create_task.\n"
)

BACKGROUND_TASKS_SECTION = (
    "\n\n## Background tasks (run_bash)\n"
    "Slow shell commands may run in a background thread when run_in_background is true "
    "or when the command looks long-running (install, build, test, etc.).\n"
    "- Set run_in_background=false to force synchronous execution and get output in the "
    "tool result immediately.\n"
    "- While a background task runs, you get a placeholder tool result; the real output "
    "is delivered later as a user message wrapped in <task_notification> XML.\n"
    "- On completion: <status>completed</status> plus an Output section — read it and "
    "continue the task.\n"
    "- On stall (interactive prompt): a statusless notification with last output — "
    "kill the task and re-run with non-interactive flags or piped input.\n"
)

TEAMS_SECTION = (
    "\n\n## Agent teams (Lead + Teammates)\n"
    "A default team is already initialized at startup — do NOT call create_team unless "
    "the user explicitly asks for a separate team name.\n"
    "- Delegate parallel work with spawn_teammate(name, role, prompt, team_name=\"\", ...).\n"
    "- Pass team_name as empty string to use the current team.\n"
    "- After spawning: tell the user the teammate is working; do NOT implement the "
    "teammate's task yourself (no write_file/edit_file for work you delegated).\n"
    "- Teammate results arrive as <teammate-message> injections or [Teammate update] "
    "notifications — summarize them for the user.\n"
    "- Use send_message to assign follow-up work; use list_teammates to check status.\n"
)

SUBAGENT_IDENTITY = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


def build_skill_section(catalog: str) -> str:
    """Skill 目录段（catalog 由 skill_load.list_skills() 生成）。"""
    return f"Skills available:\n{catalog}\nUse load_skill to get full details when needed."


MEMORY_SECTION_EMPTY = (
    f"\n\nNo memories stored yet.\n"
    f"Memory directory: {WORKDIR}/.memory\n"
    "Relevant memories may be injected into the user message when applicable.\n"
    "When the user says 'remember' or expresses a clear preference, extract it as a memory."
)

MEMORY_SECTION_WITH_INDEX = (
    f"\n\nMemories available:\n{{index}}\n"
    f"Memory directory: {WORKDIR}/.memory\n"
    "Relevant memories are injected into the latest user message when applicable.\n"
    "Respect user preferences from memory.\n"
    "When the user says 'remember' or expresses a clear preference, extract it as a memory."
)


def build_memory_section(memory_index: str) -> str:
    """Memory 索引段（拼在 identity + skill 之后）。"""
    if not memory_index.strip():
        return MEMORY_SECTION_EMPTY
    return MEMORY_SECTION_WITH_INDEX.format(index=memory_index)


def build_agent_core(skill_catalog: str) -> str:
    """主 agent：身份 + skill，不含 memory。"""
    return AGENT_IDENTITY + skill_catalog


def build_main_system(skill_catalog: str, memory_index: str) -> str:
    """主 agent 完整 system = core + memory（发送前传入最新 memory_index）。"""
    return assemble_system_prompt({
        "skill_catalog": skill_catalog,
        "memories": memory_index,
        "workspace": str(WORKDIR),
        "enabled_tools": [],
    })


def build_subagent_system(skill_catalog: str) -> str:
    """子 agent system = 身份 + skill（无 memory 段）。"""
    return assemble_system_prompt({
        "skill_catalog": skill_catalog,
        "memories": "",
        "workspace": str(WORKDIR),
        "enabled_tools": [],
    }, isSubagent=True)


# =============================================================================
# System prompt — 运行时组装与缓存
# =============================================================================

# 按主题划分的 prompt 片段；动态段（skill / memory）在 assemble 时从 context 注入
PROMPT_SECTIONS = {
    "identity": AGENT_IDENTITY,
    "task_planning": TASK_PLANNING_SECTION,
    "background_tasks": BACKGROUND_TASKS_SECTION,
    "teams": TEAMS_SECTION,
    "subagent_identity": SUBAGENT_IDENTITY,
    "workspace": f"Working directory: {WORKDIR}",
    "memory_hint": "Relevant memories may be injected into the user message when applicable.",
}


def assemble_system_prompt(context: dict, *, isSubagent: bool = False) -> str:
    """
    根据 context 选择并拼接 system prompt 各段。
    主 agent: identity + task_planning + skill_catalog + memory_section
    子 agent: subagent_identity + skill_catalog
    """
    # 身份段：主/子 agent 使用不同模板
    identity = PROMPT_SECTIONS["subagent_identity"] if isSubagent else PROMPT_SECTIONS["identity"]
    parts = [identity]
    if not isSubagent:
        parts.append(PROMPT_SECTIONS["task_planning"])
        parts.append(PROMPT_SECTIONS["background_tasks"])
        parts.append(PROMPT_SECTIONS["teams"])
    # skill 段：context 里存的是已格式化的 skill section（即 SKILL_CATALOG）
    skill_catalog = context.get("skill_catalog", "")
    if skill_catalog:
        parts.append(skill_catalog)
    # memory 段：仅主 agent；有索引则展示目录，否则展示空状态引导
    if not isSubagent:
        parts.append(build_memory_section(context.get("memories", "")))
    
    if len(parts) == 1:
        return parts[0]
    return parts[0] + "".join(parts[1:])


# 进程内缓存：context 序列化 key → 已组装的 prompt 字符串
_last_context_key: str | None = None
_last_prompt: str | None = None
_last_subagent_context_key: str | None = None
_last_subagent_prompt: str | None = None


def _context_cache_key(context: dict, *, isSubagent: bool) -> str:
    """把 context 序列化为稳定字符串，用作缓存 key"""
    payload = {**context, "_isSubagent": isSubagent}
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def get_system_prompt(context: dict, *, isSubagent: bool = False) -> str:
    """
    获取 system prompt；context 未变则直接返回缓存。
    原理：
    - json.dumps(context, sort_keys=True) 生成确定性 key，避免 hash 随机化问题
    - context 不变且已有缓存 → 跳过拼接（打印 [cache hit]）
    - context 变化 → 调用 assemble_system_prompt 并更新缓存（打印 [assembled]）
    """
    global _last_context_key, _last_prompt
    global _last_subagent_context_key, _last_subagent_prompt

    key = _context_cache_key(context, isSubagent=isSubagent)
    if isSubagent:
        if key == _last_subagent_context_key and _last_subagent_prompt:
            print("  \033[90m[cache hit] subagent system prompt unchanged\033[0m")
            return _last_subagent_prompt
        _last_subagent_context_key = key
        _last_subagent_prompt = assemble_system_prompt(context, isSubagent=True)
        loaded = ["subagent_identity", "skills"]
        print(f"  \033[32m[assembled] subagent sections: {', '.join(loaded)}\033[0m")
        return _last_subagent_prompt

    if key == _last_context_key and _last_prompt:
        print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context, isSubagent=False)
    loaded = ["identity", "task_planning", "skills"]
    if context.get("memories"):
        loaded.append("memory")
    else:
        loaded.append("memory_empty")
    print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return _last_prompt


def update_context(context: dict, messages: list) -> dict:
    """
    根据当前环境刷新 context，供 get_system_prompt 使用。
    收集：
    - skill_catalog: 启动时扫描的 skill 目录段
    - workspace: 工作区路径
    - memories: MEMORY.md 索引内容（存在且非空时写入）
    - enabled_tools: 当前注册的工具名列表
    参数 context / messages 预留给后续扩展（如按对话动态裁剪 section）。
    """
    _ = context, messages  # 暂未使用

    from memory import read_memory_index
    from skill_load import SKILL_CATALOG
    from tool import TOOL_MAP

    memories = read_memory_index()
    return {
        "skill_catalog": SKILL_CATALOG,
        "workspace": str(WORKDIR),
        "memories": memories,
        "enabled_tools": list(TOOL_MAP.keys()),
    }


# =============================================================================
# User / context 消息包装
# =============================================================================

RELEVANT_MEMORIES_OPEN = "<relevant_memories>"
RELEVANT_MEMORIES_CLOSE = "</relevant_memories>"

def wrap_relevant_memories(memories_body: str) -> str:
    return f"{RELEVANT_MEMORIES_OPEN}\n\n{memories_body}\n\n{RELEVANT_MEMORIES_CLOSE}"

def format_compacted_user_message(summary: str) -> str:
    return f"[Compacted]\n\n{summary}"

def format_reactive_compacted_user_message(summary: str) -> str:
    return f"[Reactive compact]\n\n{summary}"

def format_snipped_user_message(count: int) -> str:
    return f"[snipped {count} messages]"


# =============================================================================
# Memory — LLM 任务 prompt
# =============================================================================

SELECT_MEMORIES_TEMPLATE = """\
Given the recent conversation and the memory catalog below, \
select the indices of memories that are clearly relevant. \
Return ONLY a JSON array of integers, e.g. [0, 3]. \
If none are relevant, return [].

Recent conversation:
{recent}

Memory catalog:
{catalog}"""


EXTRACT_MEMORIES_TEMPLATE = """\
Extract user preferences, constraints, or project facts from this dialogue.
Return a JSON array. Each item: {{name, type, description, body}}.
- name: short kebab-case identifier (e.g. 'user-preference-tabs')
- type: one of 'user' (user preference), 'feedback' (guidance), \
'project' (project fact), 'reference' (external pointer)
- description: one-line summary for index lookup
- body: full detail in markdown
If nothing new or already covered by existing memories, return [].

Existing memories:
{existing}

Dialogue:
{dialogue}"""


CONSOLIDATE_MEMORIES_TEMPLATE = """\
Consolidate the following memory files. Rules:
1. Merge duplicates into one
2. Remove outdated/contradicted memories
3. Keep the total under {threshold} memories
4. Preserve important user preferences above all
Return a JSON array. Each item: {{name, type, description, body}}.

{catalog}"""


def format_select_memories(recent: str, catalog: str) -> str:
    return SELECT_MEMORIES_TEMPLATE.format(recent=recent, catalog=catalog)

def format_extract_memories(existing_desc: str, dialogue: str) -> str:
    return EXTRACT_MEMORIES_TEMPLATE.format(existing=existing_desc, dialogue=dialogue)

def format_consolidate_memories(catalog: str, threshold: int) -> str:
    return CONSOLIDATE_MEMORIES_TEMPLATE.format(threshold=threshold, catalog=catalog)

# =============================================================================
# Error recovery — LLM 错误恢复 prompt
# =============================================================================

# 到达上限后，恢复续写提示。
CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly — "
    "no apology, no recap. Pick up mid-thought."
)

# =============================================================================
# Compact — LLM 总结 prompt
# =============================================================================

COMPACT_SUMMARY_TEMPLATE = """\
Summarize this coding-agent conversation so work can continue.
Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, \
4. remaining work, 5. user constraints.
Be compact but concrete.

{conversation}"""


def format_compact_summary(conversation: str) -> str:
    return COMPACT_SUMMARY_TEMPLATE.format(conversation=conversation)


