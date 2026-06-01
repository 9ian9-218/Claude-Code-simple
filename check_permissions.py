"""权限检查模块 — 以 PreToolUse hook 形式接入 hook 系统。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

WORKDIR = Path.cwd()

# Gate 1: 硬拒绝黑名单（run_bash 专用，直接返回错误）
# Gate 2: 规则匹配（按工具名 + 条件，命中则进入 Gate 3）
# Gate 3: 用户确认（终端询问 [y/N]）

DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]

PERMISSION_RULES = [
    {
        "tools": ["write_file", "edit_file"],
        "check": lambda args: not (WORKDIR / args.get("path", "")).resolve().is_relative_to(WORKDIR),
        "message": "Writing outside workspace",
    },
    {
        "tools": ["run_bash"],
        "check": lambda args: any(
            kw in args.get("command", "")
            for kw in ["rm ", "> /etc/", "chmod 777"]
        ),
        "message": "Potentially destructive command",
    },
    {
        "tools": ["read_file"],
        "check": lambda args: any(
            s in args.get("path", "")
            for s in [".env", "credentials", "secret", "token"]
        ),
        "message": "Reading potentially sensitive file",
    },
]


def check_deny_list(command: str) -> str | None:
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Blocked: '{pattern}' is on the deny list"
    return None


def check_rules(tool_name: str, args: dict[str, Any]) -> str | None:
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None


def ask_user(tool_name: str, args: dict[str, Any], reason: str) -> str:
    print(f"\n\033[33m⚠  {reason}\033[0m")
    print(f"   Tool: {tool_name}({args})")
    choice = input("   Allow? [y/N] ").strip().lower()
    return "allow" if choice in ("y", "yes") else "deny"


def check_permission(tool_name: str, args: dict[str, Any]) -> str | None:
    """三道门权限管线，返回 None 表示通过，返回 str 表示拒绝原因。"""
    if tool_name == "run_bash":
        reason = check_deny_list(args.get("command", ""))
        if reason:
            print(f"\n\033[31m⛔ {reason}\033[0m")
            return reason

    reason = check_rules(tool_name, args)
    if reason:
        decision = ask_user(tool_name, args, reason)
        if decision == "deny":
            return f"Permission denied: {reason}"

    return None


def permission_hook(block) -> str | None:
    """
    PreToolUse hook：在工具执行前运行权限检查。

    block 需包含 .name（工具名）和 .input（参数字典）。
    返回非 None 时，trigger_hooks 会拦截本次工具调用。
    """
    return check_permission(block.name, block.input)
