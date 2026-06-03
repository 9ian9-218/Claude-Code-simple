from config import WORKDIR
import time
import json
import re
import os
from client import client

MODEL_MAX_CONTEXT_TOKENS = 600_000  # 模型理论最大上下文长度
AUTOCOMPACT_BUFFER_TOKENS = 30_000  # 预留给模型输出、tools schema、token 估算误差（约窗口 5%）
# ===== L3: tool result budget =====
BUDGET_MAX_TOKENS = 120_000             # L3: 同一轮 tool result 总 token 预算，约窗口 20%）
PERSIST_THRESHOLD_TOKENS = 6_000        # L3: 单条 tool result 落盘阈值
PREVIEW_TOKENS = 500                    # L3: 落盘后预览 token 数
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
# ===== L1: snip =====
MAX_NUM_MESSAGES = 240                  # L1: 消息条数上限
# ===== L2: micro compact =====
MICRO_COMPACT_MAX_MESSAGE_TOKENS = 8_000    #L2: 旧 tool result 被替换的 token 阈值
MICRO_COMPACT_KEEP_RECENT_TOOL_RESULTS = 10 #L2: 保留最近 tool result 数
# ===== L4: auto / reactive compact =====
CONTEXT_LIMIT = 480_000 # 触发 L4 全量压缩的阈值（窗口 80%）
AUTO_COMPACT_MAX_INPUT_TOKENS_EST = 240_000   # L4: 送总结的最大输入 token，（约窗口 40%）
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 12_000      #L4: 总结输出 token 上限
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
#reactivte config
MAX_REACTIVE_RETRIES=2

# ----- 参考：1M 窗口对应值 -----
# MODEL_MAX_CONTEXT_TOKENS = 1_000_000
# AUTOCOMPACT_BUFFER_TOKENS = 50_000
# CONTEXT_LIMIT = 800_000
# PERSIST_THRESHOLD_TOKENS = 8_000
# BUDGET_MAX_TOKENS = 200_000
# MAX_NUM_MESSAGES = 300
# MICRO_COMPACT_MAX_MESSAGE_TOKENS = 12_000
# MICRO_COMPACT_KEEP_RECENT_TOOL_RESULTS = 15
# AUTO_COMPACT_MAX_INPUT_TOKENS_EST = 350_000
# MAX_OUTPUT_TOKENS_FOR_SUMMARY = 16_000


def estimate_tokens(text: str) -> int:
    """
    本地估算 token 数（不调用 API）。
    DeepSeek / OpenAI 类 BPE 的简化启发式。
    """
    if not isinstance(text, str):
        text = str(text)
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    english_alnum = len(re.findall(r"[A-Za-z0-9]", text))
    other_chars = len(text) - chinese_chars - english_alnum
    tokens = (chinese_chars * 0.6) + (english_alnum * 0.28) + (other_chars * 0.2)
    return max(1, int(tokens) + 1)

def estimate_message_tokens(msg) -> int:
    return estimate_tokens(json.dumps(msg, default=str))

def estimate_messages_tokens(messages) -> int:
    return sum(estimate_message_tokens(m) for m in messages)


def _split_rounds(body: list) -> list[list]:
    """Split body into rounds; assistant+tool_calls includes all following tool messages."""
    rounds: list[list] = []
    i, n = 0, len(body)
    while i < n:
        start = i
        msg = body[i]
        i += 1
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            while i < n and body[i].get("role") == "tool":
                i += 1
        rounds.append(body[start:i])
    return rounds


def _flatten_rounds(rounds: list[list]) -> list:
    out: list = []
    for r in rounds:
        out.extend(r)
    return out


def _validate_tool_pairing(messages) -> None:
    """Raise if assistant tool_calls are not fully answered by following tool messages."""
    i, n = 0, len(messages)
    while i < n:
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            expected = {tc["id"] for tc in msg["tool_calls"]}
            i += 1
            seen: set[str] = set()
            while i < n and messages[i].get("role") == "tool":
                seen.add(messages[i].get("tool_call_id"))
                i += 1
            if expected != seen:
                raise ValueError(
                    f"tool_call pairing broken: expected {expected}, got {seen}"
                )
        else:
            i += 1


# L1: snipCompact — trim middle messages (实际的第二步)
def snip_compact(messages, max_messages=MAX_NUM_MESSAGES):
    if len(messages) <= max_messages:
        return messages

    prefix, body = [], messages
    if messages and messages[0].get("role") == "system":
        prefix, body = [messages[0]], messages[1:]

    rounds = _split_rounds(body)
    if len(rounds) <= 1:
        return messages

    # 从 head / tail 各保留若干完整 round，中间用占位符替换
    head_idx = 0
    head_len = len(prefix)
    for r in rounds:
        next_len = head_len + len(r)
        if next_len + 1 + 1 > max_messages:  # 留 1 占位 + 至少 1 条 tail
            break
        head_len = next_len
        head_idx += 1

    if head_idx == 0:
        head_idx = 1
        head_len = len(prefix) + len(rounds[0])

    tail_budget = max_messages - head_len - 1
    tail_rounds: list[list] = []
    tail_len = 0
    for r in reversed(rounds[head_idx:]):
        if tail_len + len(r) > tail_budget and tail_rounds:
            break
        tail_rounds.insert(0, r)
        tail_len += len(r)

    if not tail_rounds:
        tail_rounds = [rounds[-1]]
        tail_len = len(tail_rounds[0])

    tail_idx = len(rounds) - len(tail_rounds)
    if tail_idx <= head_idx:
        return messages

    while tail_idx > head_idx + 1:
        snipped = sum(len(r) for r in rounds[head_idx:tail_idx])
        result = list(prefix)
        result.extend(_flatten_rounds(rounds[:head_idx]))
        from prompt import format_snipped_user_message
        result.append({"role": "user", "content": format_snipped_user_message(snipped)})
        result.extend(_flatten_rounds(rounds[tail_idx:]))
        try:
            _validate_tool_pairing(result)
        except ValueError:
            return messages
        if len(result) <= max_messages:
            return result
        tail_idx -= 1

    return messages


# L2: microCompact — old result placeholders (实际的第三步)
def collect_tool_results(messages):
    """OpenAI-style: role=tool, content=str, tool_call_id on message."""
    results = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        results.append((mi, msg))
    return results


def micro_compact(messages):
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= MICRO_COMPACT_KEEP_RECENT_TOOL_RESULTS:
        return messages
    for _, msg in tool_results[:-MICRO_COMPACT_KEEP_RECENT_TOOL_RESULTS]:
        if estimate_tokens(msg["content"]) > MICRO_COMPACT_MAX_MESSAGE_TOKENS:
            msg["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


# L3: toolResultBudget — persist large results to disk (实际的第一步)
def _trailing_tool_messages(messages):
    """Return indices of the trailing consecutive tool messages (current turn batch)."""
    indices = []
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "tool" or not isinstance(msg.get("content"), str):
            break
        indices.append(i)
    return list(reversed(indices))

def truncate_to_tokens(text: str, max_tokens: int) -> str:
    if estimate_tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    suffix = "..." if lo < len(text) else ""
    return text[:lo] + suffix

def persist_large_output(tool_call_id, output):
    if estimate_tokens(output) <= PERSIST_THRESHOLD_TOKENS:
        return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_call_id}.txt"
    if not path.exists():
        path.write_text(output)
    preview = truncate_to_tokens(output, PREVIEW_TOKENS)
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{preview}\n</persisted-output>"


def tool_result_budget(messages, max_tokens=BUDGET_MAX_TOKENS):
    indices = _trailing_tool_messages(messages)
    if not indices:
        return messages
    tool_msgs = [messages[i] for i in indices]
    total_tokens = sum(estimate_tokens(msg["content"]) for msg in tool_msgs)
    if total_tokens <= max_tokens:
        return messages
    ranked = sorted(tool_msgs, key=lambda m: estimate_tokens(m["content"]), reverse=True)
    for msg in ranked:
        if total_tokens <= max_tokens:
            break
        content = msg["content"]
        if estimate_tokens(content) <= PERSIST_THRESHOLD_TOKENS:
            continue
        tid = msg.get("tool_call_id", "unknown")
        msg["content"] = persist_large_output(tid, content)
        total_tokens = sum(estimate_tokens(m["content"]) for m in tool_msgs)
    return messages


# L4: autoCompact — LLM full summary (实际的第四步)
def write_transcript(messages):
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    return path


def summarize_history(messages):
    total_est = estimate_messages_tokens(messages)
    if total_est > AUTO_COMPACT_MAX_INPUT_TOKENS_EST:
        truncated = []
        running = 0
        for msg in reversed(messages):
            sz = estimate_message_tokens(msg)
            if running + sz > AUTO_COMPACT_MAX_INPUT_TOKENS_EST:
                break
            truncated.insert(0, msg)
            running += sz
        messages_to_summarize = truncated
    else:
        messages_to_summarize = messages
    conversation = json.dumps(messages_to_summarize, default=str)
    from prompt import format_compact_summary
    prompt = format_compact_summary(conversation)
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL"),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=MAX_OUTPUT_TOKENS_FOR_SUMMARY,
    )
    return response.choices[0].message.content or "(empty summary)"


def compact_history(messages):
    transcript_path = write_transcript(messages)
    print(f"[transcript saved: {transcript_path}]")
    summary = summarize_history(messages)
    from prompt import format_compacted_user_message
    return [{"role": "user", "content": format_compacted_user_message(summary)}]


# Emergency: reactiveCompact — on API error
def reactive_compact(messages):
    write_transcript(messages)
    summary = summarize_history(messages)
    from prompt import format_reactive_compacted_user_message
    return [{"role": "user", "content": format_reactive_compacted_user_message(summary)}, *messages[-5:]]
