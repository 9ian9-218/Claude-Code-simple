"""Thread-local agent identity (team name, agent name, role)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal

from teammates.constants import TEAM_LEAD_NAME

AgentRole = Literal["lead", "teammate", "subagent"]


@dataclass
class AgentContext:
    team_name: str | None = None
    agent_name: str = TEAM_LEAD_NAME
    agent_id: str | None = None
    color: str | None = None
    role: AgentRole = "lead"
    agent_type: str = "general-purpose"

    @property
    def is_lead(self) -> bool:
        return self.role == "lead"

    @property
    def is_teammate(self) -> bool:
        return self.role == "teammate"

    @property
    def is_subagent(self) -> bool:
        return self.role == "subagent"

    @property
    def is_worker(self) -> bool:
        return self.role in ("teammate", "subagent")


_local = threading.local()

def get_agent_context() -> AgentContext:
    ctx = getattr(_local, "ctx", None)
    if ctx is None:
        ctx = AgentContext()
        _local.ctx = ctx
    return ctx


def set_agent_context(ctx: AgentContext) -> None:
    _local.ctx = ctx


def reset_agent_context() -> None:
    _local.ctx = AgentContext()


class agent_context:
    """
    agent_context 是一个上下文管理器（context manager），
    用于“临时切换” agent 的身份信息（即 AgentContext）。
    在 with 语句块中，它将全局线程本地的 agent context 替换为给定 ctx，
    块结束后再恢复到原来的 context。
    用法示例:
        old_ctx = get_agent_context()
        with agent_context(new_ctx):
            # 这里 get_agent_context() 返回 new_ctx
            ...
        # 离开 with 后，get_agent_context() 又恢复为 old_ctx
    主要用途：
    - 测试、模拟、权限隔离等需要切换身份的场景。
    """

    def __init__(self, ctx: AgentContext):
        # _new 保存要切换进来的 context
        self._new = ctx
        # _prev 用于后续恢复现场，进入时会记录原有 context
        self._prev: AgentContext | None = None

    def __enter__(self) -> AgentContext:
        # 进入 with 时，保存现有 context，并设为新的
        self._prev = get_agent_context()
        set_agent_context(self._new)
        return self._new

    def __exit__(self, *_args) -> None:
        # 离开 with 时，还原原 context（如果有）
        if self._prev is not None:
            set_agent_context(self._prev)
