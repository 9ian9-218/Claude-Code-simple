from config import WORKDIR
import time
import json
from client import client
import os
from dotenv import load_dotenv
load_dotenv()

MODEL_MAX_CONTEXT_TOKENS = 500000

AUTOCOMPACT_BUFFER_TOKENS=13000
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES=3
MAX_OUTPUT_TOKENS_FOR_SUMMARY=20000
POST_COMPACT_TOKEN_BUDGET=50000
POST_COMPACT_MAX_FILES_TO_RESTORE=5
POST_COMPACT_MAX_TOKENS_PER_FILE=5000
MAX_COMPACT_STREAMING_RETRIES=2

CONTEXT_LIMIT = 50000

#BUDGET_CONFIG
PERSIST_THRESHOLD = 30000
BUDGET_MAX_BYTES = 600_000
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"

#SNIP_CONFIG
MAX_NUM_MESSAGES = 50

#MICRO_COMPACT_CONFIG
MICRO_COMPACT_MAX_MESSAGE_LENGTH = 500
MICRO_COMPACT_KEEP_RECENT_TOOL_RESULTS = 3
MICRO_COMPACT_INTERVAL=60

#AUTO_COMPACT_CONFIG
AUTO_COMPACT_MAX_INPUT_BYTES = 600_000

# L1: snipCompact — trim middle messages(实际的第二步)
def snip_compact(messages, max_messages=MAX_NUM_MESSAGES):
    if len(messages) <= max_messages: return messages
    keep_head, keep_tail = 3, max_messages - 3
    snipped = len(messages) - keep_head - keep_tail
    return messages[:keep_head] + [{"role": "user", "content": f"[snipped {snipped} messages]"}] + messages[-keep_tail:]

# L2: microCompact — old result placeholders（实际的第三步）
def collect_tool_results(messages):
    """Collect OpenAI-style tool messages: role=tool, content=str, tool_call_id on message."""
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
    if len(tool_results) <= MICRO_COMPACT_KEEP_RECENT_TOOL_RESULTS: return messages
    for _, msg in tool_results[:-MICRO_COMPACT_KEEP_RECENT_TOOL_RESULTS]:
        if len(msg["content"]) > MICRO_COMPACT_MAX_MESSAGE_LENGTH:
            msg["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


# L3: toolResultBudget — persist large results to disk（实际的第一步）
def _trailing_tool_messages(messages):
    """Return indices of the trailing consecutive tool messages (current turn batch)."""
    indices = []
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "tool" or not isinstance(msg.get("content"), str):
            break
        indices.append(i)
    return list(reversed(indices))

def persist_large_output(tool_call_id, output):
    if len(output) <= PERSIST_THRESHOLD: return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_call_id}.txt"
    if not path.exists(): path.write_text(output)
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"

def tool_result_budget(messages, max_bytes=BUDGET_MAX_BYTES):
    indices = _trailing_tool_messages(messages)
    if not indices: return messages
    tool_msgs = [messages[i] for i in indices]
    total = sum(len(msg["content"]) for msg in tool_msgs)
    if total <= max_bytes: return messages
    ranked = sorted(tool_msgs, key=lambda m: len(m["content"]), reverse=True)
    for msg in ranked:
        if total <= max_bytes: break
        content = msg["content"]
        if len(content) <= PERSIST_THRESHOLD: continue
        tid = msg.get("tool_call_id", "unknown")
        msg["content"] = persist_large_output(tid, content)
        total = sum(len(m["content"]) for m in tool_msgs)
    return messages


# L4: autoCompact — LLM full summary(实际的第四步)
def write_transcript(messages):
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w") as f:
        for msg in messages: f.write(json.dumps(msg, default=str) + "\n")
    return path

def summarize_history(messages):
    conversation = json.dumps(messages, default=str)[:AUTO_COMPACT_MAX_INPUT_BYTES]
    prompt = ("Summarize this coding-agent conversation so work can continue.\n"
              "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
              "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n" + conversation)
    response = client.chat.completions.create(model=os.getenv("OPENAI_MODEL"), messages=[{"role": "user", "content": prompt}], max_tokens=2000)
    return response.choices[0].message.content or "(empty summary)"

def compact_history(messages):
    transcript_path = write_transcript(messages)
    print(f"[transcript saved: {transcript_path}]")
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


# Emergency: reactiveCompact — on API error
def reactive_compact(messages):
    transcript = write_transcript(messages)
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *messages[-5:]]

