from openai import OpenAI
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall,
    Function,
)
from tool import get_all_tools
import os
import sys
from prompt import get_system_prompt, update_context
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url=os.getenv("OPENAI_BASE_URL"),
)

class AssistantMessage:
    def __init__(self, content, tool_calls, needs_follow_up, finish_reason):
        self.content = content
        self.tool_calls = tool_calls
        self.needs_follow_up = needs_follow_up
        self.finish_reason = finish_reason

    def model_dump(self, exclude_none=True):
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in self.tool_calls
            ]
        if exclude_none:
            d = {k: v for k, v in d.items() if v is not None}
        return d


_initial_context = update_context({}, [])
SYSTEM = get_system_prompt(_initial_context)
SUBAGENT_SYSTEM = get_system_prompt(_initial_context, isSubagent=True)


def _ensure_system(messages, content: str) -> None:
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = content
        return
    messages.insert(0, {"role": "system", "content": content})


def send_messages(messages, max_tokens=8000, isSubagent=False, model=None):
    context = update_context({}, messages)
    system_prompt = get_system_prompt(context, isSubagent=isSubagent)
    _ensure_system(messages, system_prompt)
    stream = client.chat.completions.create(
        model=model or os.getenv("OPENAI_MODEL"),
        messages=messages,
        tools=get_all_tools(isSubagent),
        tool_choice="auto",
        stream=True,
        max_tokens=max_tokens,
    )
    needs_follow_up = False
    finish_reason = None
    content_parts = []
    tool_calls_acc = {}
    if not isSubagent:
        sys.stdout.write("Model >\t ")
        sys.stdout.flush()

    for chunk in stream:
        choice = chunk.choices[0]
        if choice.finish_reason:
            finish_reason = choice.finish_reason

        delta = choice.delta
        if delta.content:
            sys.stdout.write(delta.content)
            sys.stdout.flush()
            content_parts.append(delta.content)

        if delta.tool_calls:
            needs_follow_up = True
            for tc in delta.tool_calls:
                idx = tc.index
                if idx not in tool_calls_acc:
                    tool_calls_acc[idx] = {
                        "id": "",
                        "function": {"name": "", "arguments": ""},
                    }
                if tc.id:
                    tool_calls_acc[idx]["id"] = tc.id
                if tc.function and tc.function.name:
                    tool_calls_acc[idx]["function"]["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    tool_calls_acc[idx]["function"]["arguments"] += tc.function.arguments

    if not isSubagent:
        sys.stdout.write("\n")
        sys.stdout.flush()

    tool_calls = None
    if tool_calls_acc:
        tool_calls = [
            ChatCompletionMessageToolCall(
                id=tool_calls_acc[idx]["id"],
                type="function",
                function=Function(
                    name=tool_calls_acc[idx]["function"]["name"],
                    arguments=tool_calls_acc[idx]["function"]["arguments"],
                ),
            )
            for idx in sorted(tool_calls_acc)
        ]

    content = "".join(content_parts) or None
    return AssistantMessage(content, tool_calls, needs_follow_up, finish_reason)
