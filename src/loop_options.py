"""Agent loop runtime options — decouple identity, I/O, and feature flags."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LoopOptions:
    preserve_system: bool = False
    inject_lead_notifications: bool = True
    inject_background_notifications: bool = True
    enable_memory: bool = True
    enable_background: bool = True
    quiet_output: bool = False
    exit_on_final_content: bool = False
    skip_memory_stop_hook: bool = False

    @classmethod
    def lead(cls) -> LoopOptions:
        return cls()

    @classmethod
    def subagent(cls) -> LoopOptions:
        return cls(
            inject_lead_notifications=False,
            inject_background_notifications=False,
            enable_memory=False,
            enable_background=False,
            quiet_output=True,
            exit_on_final_content=True,
            skip_memory_stop_hook=True,
        )

    @classmethod
    def teammate(cls) -> LoopOptions:
        return cls(
            preserve_system=True,
            inject_lead_notifications=False,
            inject_background_notifications=True,
            enable_memory=False,
            enable_background=True,
            quiet_output=True,
            exit_on_final_content=True,
            skip_memory_stop_hook=True,
        )

    @classmethod
    def from_legacy_is_subagent(cls, is_subagent: bool) -> LoopOptions:
        return cls.subagent() if is_subagent else cls.lead()
