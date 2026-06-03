"""
error_recovery.py — LLM 调用错误恢复

三条恢复路径：
  Path 1: max_tokens（OpenAI finish_reason=length）→ 提升 max_tokens / continuation prompt
  Path 2: prompt_too_long → reactive compact（一次）
  Path 3: 429/529 → with_retry 指数退避 + 可选 fallback model
"""
"""
教学版只展开了前 5 种（最常见的），其余各有专门处理逻辑。。CC 实际有十几种 reason/transition，每轮 LLM 调用后都会判断：

reason/transition   | 教学版对应  | Claude Code 行为
|---|---|---|
| `completed` | 正常完成 | 返回结果 |
| `next_turn` | 正常工具调用 | 继续下一轮工具执行 |
| `max_output_tokens_escalate` | 路径 1 | 8K→64K 升级 |
| `max_output_tokens_recovery` | 路径 1 续写 | 续写提示（最多 3 次） |
| `reactive_compact_retry` | 路径 2 | reactive compact → 重试 |
| `prompt_too_long` | 路径 2 | 同上 |
| `collapse_drain_retry` | 未展开 | context collapse 先提交暂存 |
| `model_error` | 未展开 | 重试 |
| `image_error` | 未展开 | ImageSizeError / ImageResizeError 专门处理 |
| `aborted_streaming` | 未展开 | 流式中止恢复 |
| `aborted_tools` | 未展开 | 工具中止 |
| `stop_hook_blocking` | 未展开 | 注入 blocking error → 模型自纠 |
| `stop_hook_prevented` | 未展开 | hooks 阻止 |
| `hook_stopped` | 未展开 | hook 停止执行 |
| `token_budget_continuation` | 未展开 | token 用量 < 90% 时继续 |
| `blocking_limit` | 未展开 | 阻塞限制 |
| `max_turns` | 未展开 | 达到最大轮次 |
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Any, Literal

import compact

ESCALATED_MAX_TOKENS = 64_000
DEFAULT_MAX_TOKENS = 8_000
MAX_RECOVERY_RETRIES = 3
MAX_RETRIES = 10
BASE_DELAY_MS = 500
MAX_CONSECUTIVE_529 = 3


class RecoveryState:
    """Track recovery state across agent_loop iterations."""
    def __init__(self) -> None:
        self.has_escalated = False
        self.recovery_count = 0
        self.consecutive_529 = 0
        self.has_attempted_reactive_compact = False
        self.current_model = os.getenv("OPENAI_MODEL")


def retry_delay(attempt: int, retry_after: float | None = None) -> float:
    """指数退避 + jitter；Retry-After 优先。"""
    if retry_after:
        return retry_after
    base = min(BASE_DELAY_MS * (2**attempt), 32_000) / 1000
    jitter = random.uniform(0, base * 0.25)
    return base + jitter


def with_retry(fn, state: RecoveryState):
    """
    该函数用于处理429/529等暂时性错误，实现指数退避重试机制。
    - 对429（速率限制）错误：采用指数退避（exponential backoff）+抖动（jitter），等待一段时间后重试。
    - 对529（服务过载）错误：同样指数退避，并在连续多次529后尝试切换到备用模型（如配置 Fallback Model）。
    - 非暂时性错误直接抛给外围逻辑处理。
    - 若重试次数超过最大次数（MAX_RETRIES），则抛出RuntimeError。
    参数说明：
        fn: 调用的LLM函数
        state: RecoveryState对象，跟踪恢复状态
    返回：
        若成功调用，则返回fn()的结果。
    """
    fallback_model = os.getenv("FALLBACK_MODEL_ID")
    for attempt in range(MAX_RETRIES):
        try:
            result = fn()
            state.consecutive_529 = 0
            return result
        except Exception as e:
            name = type(e).__name__
            msg = str(e).lower()

            # 429 rate limit -> exponential backoff
            if "ratelimit" in name.lower() or "429" in msg:
                delay = retry_delay(attempt)
                print(
                    f"  \033[33m[429 rate limit] retry {attempt + 1}/{MAX_RETRIES},"
                    f" wait {delay:.1f}s\033[0m"
                )
                time.sleep(delay)
                continue

            # 529 overloaded -> exponential backoff + fallback model
            if "overloaded" in name.lower() or "529" in msg or "overloaded" in msg:
                state.consecutive_529 += 1
                if state.consecutive_529 >= MAX_CONSECUTIVE_529:
                    if fallback_model:
                        state.current_model = fallback_model
                        state.consecutive_529 = 0
                        print(
                            f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                            f" switching to {fallback_model}\033[0m"
                        )
                    else:
                        state.consecutive_529 = 0
                        print(
                            f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                            f" no FALLBACK_MODEL_ID configured, continuing retry\033[0m"
                        )
                delay = retry_delay(attempt)
                print(
                    f"  \033[33m[529 overloaded] retry {attempt + 1}/{MAX_RETRIES},"
                    f" wait {delay:.1f}s\033[0m"
                )
                time.sleep(delay)
                continue

            # Not transient -> re-raise for outer try/except
            raise
    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded")


def is_prompt_too_long_error(e: Exception) -> bool:
    """Check whether an API error indicates prompt/context too long."""
    msg = str(e).lower()
    return (
        ("prompt" in msg and "long" in msg)
        or "prompt_is_too_long" in msg
        or "prompt_too_long" in msg
        or "context_length_exceeded" in msg
        or "max_context_window" in msg
        or "too many tokens" in msg
    )


def _is_max_tokens_finish(finish_reason: str | None) -> bool:
    """OpenAI: length；Anthropic: max_tokens。"""
    return finish_reason in ("length", "max_tokens")


def _append_error_message(messages: list, text: str) -> None:
    messages.append({"role": "assistant", "content": f"[Error] {text}"})


@dataclass
class LLMInvokeResult:
    action: Literal["success", "retry", "abort"]
    message: Any = None
    max_tokens: int | None = None


def send_messages_with_recovery(
    *,
    request_messages: list,
    messages: list,
    state: RecoveryState,
    max_tokens: int,
    isSubagent: bool = False,
) -> LLMInvokeResult:
    """
    带错误恢复的 LLM 调用（agent_loop 统一入口）。
    - success: message 可用，走正常 tool/结束 分支
    - retry: 已调整 messages 或 max_tokens，caller 应 continue 重试
    - abort: 不可恢复，caller 应结束本轮
    """
    from client import send_messages
    from prompt import CONTINUATION_PROMPT

    try:
        message = with_retry(
            lambda: send_messages(
                request_messages,
                max_tokens=max_tokens,
                isSubagent=isSubagent,
                model=state.current_model,
            ),
            state,
        )
    except Exception as e:
        # ——— Path 2: prompt/context太长（通常为请求过大，窗口溢出） ———
        if is_prompt_too_long_error(e):
            if not state.has_attempted_reactive_compact:
                print("  \033[31m[reactive compact]\033[0m")
                messages[:] = compact.reactive_compact(messages)
                state.has_attempted_reactive_compact = True
                return LLMInvokeResult(action="retry")
            print("  \033[31m[unrecoverable] still too long after compact\033[0m")
            _append_error_message(messages, "Context too large, cannot continue.")
            return LLMInvokeResult(action="abort")
        # ——— 其它异常不可恢复，写入error信息，返回abort ———
        name = type(e).__name__
        print(f"  \033[31m[unrecoverable] {name}: {str(e)[:100]}\033[0m")
        _append_error_message(messages, f"{name}: {str(e)[:200]}")
        return LLMInvokeResult(action="abort")

    # ——— Path 1: LLM输出被截断（max_tokens耗尽），需动态升级max_tokens或提示续写 ———
    if _is_max_tokens_finish(message.finish_reason):
        if not state.has_escalated:
            new_max = ESCALATED_MAX_TOKENS
            state.has_escalated = True
            print(
                f"  \033[33m[max_tokens] escalating"
                f" {max_tokens} -> {new_max}\033[0m"
            )
            return LLMInvokeResult(action="retry", max_tokens=new_max)

        messages.append(message.model_dump(exclude_none=True))
        if state.recovery_count < MAX_RECOVERY_RETRIES:
            messages.append({"role": "user", "content": CONTINUATION_PROMPT})
            state.recovery_count += 1
            print(
                f"  \033[33m[max_tokens] continuation"
                f" {state.recovery_count}/{MAX_RECOVERY_RETRIES}\033[0m"
            )
            return LLMInvokeResult(action="retry", max_tokens=max_tokens)

        print("  \033[31m[max_tokens] recovery limit reached\033[0m")
        return LLMInvokeResult(action="abort")

    return LLMInvokeResult(action="success", message=message)
