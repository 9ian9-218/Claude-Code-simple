from config import WORKDIR
import time
import json
import re
import os
from dotenv import load_dotenv
from client import client

load_dotenv()

# ===== 1M context (DeepSeek) =====
MODEL_MAX_CONTEXT_TOKENS = 1_000_000
# 预留给模型输出、tools schema、token 估算误差
AUTOCOMPACT_BUFFER_TOKENS = 50_000
# 超过此值触发 L4 全量 LLM 总结（约窗口 80%）
CONTEXT_LIMIT = 800_000

# ===== L3: tool result budget =====
PERSIST_THRESHOLD_TOKENS = 8_000       # 单条 tool 结果超过此值才持久化到磁盘
BUDGET_MAX_TOKENS = 200_000            # 同轮 tool 结果总 token 预算
PREVIEW_TOKENS = 500                   # 持久化后 preview 的 token 上限
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"

# ===== L1: snip =====
MAX_NUM_MESSAGES = 200                 # 消息条数上限（1M 下可保留更多轮次）

# ===== L2: micro compact =====
MICRO_COMPACT_MAX_MESSAGE_TOKENS = 12_000
MICRO_COMPACT_KEEP_RECENT_TOOL_RESULTS = 15

# ===== L4: auto / reactive compact =====
AUTO_COMPACT_MAX_INPUT_TOKENS_EST = 350_000
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 16_000
TRANSCRIPT_DIR = WORKDIR / ".transcripts"


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



# L1: snipCompact — trim middle messages (实际的第二步)
def snip_compact(messages, max_messages=MAX_NUM_MESSAGES):
    if len(messages) <= max_messages:
        return messages
    keep_head, keep_tail = 3, max_messages - 3
    snipped = len(messages) - keep_head - keep_tail
    return (
        messages[:keep_head]
        + [{"role": "user", "content": f"[snipped {snipped} messages]"}]
        + messages[-keep_tail:]
    )


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
    prompt = (
        "Summarize this coding-agent conversation so work can continue.\n"
        "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
        "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n"
        + conversation
    )
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
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


# Emergency: reactiveCompact — on API error
def reactive_compact(messages):
    write_transcript(messages)
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *messages[-5:]]
