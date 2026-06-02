"""
prompt.py — 项目内所有 prompt 模板的统一入口。

组装规则（主 agent system）:
  AGENT_IDENTITY + skill_catalog + memory_section

子 agent:
  SUBAGENT_IDENTITY + skill_catalog
"""

from config import WORKDIR

# =============================================================================
# System prompt — 主 agent
# =============================================================================

AGENT_IDENTITY = (
    f"You are a coding agent at {WORKDIR}. "
    "Before starting any multi-step task, use todo_write to plan your steps and update status as you go. "
    "For complex sub-problems, use the task tool to spawn a subagent."
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
    "\n\nNo memories stored yet.\n"
    "Relevant memories may be injected into the user message when applicable.\n"
    "When the user says 'remember' or expresses a clear preference, extract it as a memory."
)

MEMORY_SECTION_WITH_INDEX = (
    "\n\nMemories available:\n{index}\n"
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
    return build_agent_core(skill_catalog) + build_memory_section(memory_index)


def build_subagent_system(skill_catalog: str) -> str:
    """子 agent system = 身份 + skill（无 memory 段）。"""
    return SUBAGENT_IDENTITY + skill_catalog


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
