from config import WORKDIR
import time
import json
from client import client
import os
from dotenv import load_dotenv
load_dotenv()

# 模型最大上下文 token 数
MODEL_MAX_CONTEXT_TOKENS = 950_000  # 未使用
# 总结输出允许的最大 token 数（用于 LLM 总结时的输出 token 限制，未使用）
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20000  # 未使用
# compact 后想保留的 token 预算（最终上下文最大长度，未使用）
POST_COMPACT_TOKEN_BUDGET = 50000  # 未使用
# compact 后要恢复的最大文件数（未使用）
POST_COMPACT_MAX_FILES_TO_RESTORE = 5  # 未使用
# compact 后每个文件允许恢复的最大 token 数（未使用）
POST_COMPACT_MAX_TOKENS_PER_FILE = 5000  # 未使用


# ===== BUDGET_CONFIG =====
PREVIEW_LENGTH = 2000  # 超大 output 持久化时，preview 截取长度（已用在输出预览内容的截断，可用于 persist_large_output）
PERSIST_THRESHOLD_TOKENS = 5_000  # 判定为“过大 output”进行持久化时的 token 阈值
BUDGET_MAX_TOKENS = 200_000  # 同轮结果总 token 预算，即允许的 tool_result 总长度（已用，用于 tool_result_budget 限制总内容）
TRANSCRIPT_DIR = WORKDIR / ".transcripts"  # 暂未使用：历史转录文件输出目录  # 未使用
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"  # 持久化大结果的目录（已用，用于持久化 output 到磁盘）

# ===== SNIP 配置 =====
MAX_NUM_MESSAGES = 50  # 允许的最大 message 条数，超过就截断中间部分

# ===== MICRO COMPACT 配置 =====
MICRO_COMPACT_MAX_MESSAGE_TOKENS = 2_000  # 允许的 tool_result 最长 token
MICRO_COMPACT_KEEP_RECENT_TOOL_RESULTS = 3  # micro compact 要保留的新 tool_result 数
MICRO_COMPACT_INTERVAL = 60  # micro compact 的自动处理间隔（minutes）（未使用）

# ===== AUTO COMPACT 配置 =====
AUTOCOMPACT_BUFFER_TOKENS = 13000  # 自动 compact 时，预留给 buffer 的最小 token 数（未使用）
AUTO_COMPACT_MAX_INPUT_TOKENS_EST = 200_000  # 自动 compact 时输入最大 token 粗略估算（未使用）
CONTEXT_LIMIT = 600_000  # 总上下文 token 限制（已用，用于判断是否触发 full summary/compact）
MAX_COMPACT_STREAMING_RETRIES = 2  # 自动 compact 失败后最大重试次数（未使用）
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3  # 连续失败最大次数（未使用）

import re
def estimate_tokens(text: str) -> int:
    """
    本地估算文本的 token 数量（不调用 API）。
    策略：英文按单词数*1.3，中文按字符数*0.6，混合取平均。
    DeepSeek/OpenAI 类 BPE tokenizer 的简化模拟。
    """
    if not isinstance(text, str):
        text = str(text)
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    english_alnum = len(re.findall(r'[A-Za-z0-9]', text))
    # 中文字符平均 1.5~2 字符 -> 1 token，取 0.6 系数（即 1 字符≈0.6 token）
    # 英文+数字平均 3-4 字符 -> 1 token，取 0.28 系数
    # 其他符号、空格粗略按 1 字符 -> 0.2 token
    other_chars = len(text) - chinese_chars - english_alnum
    tokens = (chinese_chars * 0.6) + (english_alnum * 0.28) + (other_chars * 0.2)
    return max(1, int(tokens) + 1)# 确保最小值，向上取整


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
    if len(tool_results) <= MICRO_COMPACT_KEEP_RECENT_TOOL_RESULTS:
        return messages
    for _, msg in tool_results[:-MICRO_COMPACT_KEEP_RECENT_TOOL_RESULTS]:
        if estimate_tokens(msg["content"]) > MICRO_COMPACT_MAX_MESSAGE_TOKENS:
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
    if len(output) <= PERSIST_THRESHOLD_TOKENS: return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_call_id}.txt"
    if not path.exists(): path.write_text(output)
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:PREVIEW_LENGTH]}\n</persisted-output>"

def tool_result_budget(messages, max_tokens=BUDGET_MAX_TOKENS):
    indices = _trailing_tool_messages(messages)
    if not indices:
        return messages
    tool_msgs = [messages[i] for i in indices]
    total_tokens = sum(estimate_tokens(msg["content"]) for msg in tool_msgs)
    if total_tokens <= max_tokens:
        return messages
    # 按 token 数降序排序
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



# L4: autoCompact — LLM full summary(实际的第四步)
def estimate_size(msgs): return len(str(msgs))

def write_transcript(messages):
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w") as f:
        for msg in messages: f.write(json.dumps(msg, default=str) + "\n")
    return path

def summarize_history(messages):
    total_est = sum(estimate_tokens(json.dumps(msg, default=str)) for msg in messages)
    if total_est > AUTO_COMPACT_MAX_INPUT_TOKENS_EST:
        truncated = []
        running = 0
        for msg in reversed(messages):
            sz = estimate_tokens(json.dumps(msg, default=str))
            if running + sz > AUTO_COMPACT_MAX_INPUT_TOKENS_EST:
                break
            truncated.insert(0, msg)
            running += sz
        messages_to_summarize = truncated
    else:
        messages_to_summarize = messages
    conversation = json.dumps(messages_to_summarize, default=str)
    prompt = ("Summarize this coding-agent conversation so work can continue.\n"
              "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
              "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n" + conversation)
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL"),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=MAX_OUTPUT_TOKENS_FOR_SUMMARY
    )
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

