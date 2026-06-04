"""
messageQueueManager.py — 共享命令队列（对齐 CC messageQueueManager.ts）

后台任务、cron 等异步事件通过 enqueuePendingNotification / enqueueTaskNotification
入队；agent_loop 在每轮 LLM 调用前通过 consume_pending_notifications 消费。

优先级：next > later。后台任务默认 later，不阻塞用户输入。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum


class Priority(str, Enum):
    NEXT = "next"
    LATER = "later"


@dataclass(frozen=True)
class PendingNotification:
    content: str
    priority: Priority
    recipient: str | None = None


_lock = threading.Lock()
_next_queue: list[PendingNotification] = []
_later_queue: list[PendingNotification] = []


def enqueue_pending_notification(
    content: str,
    priority: str = "later",
    *,
    recipient: str | None = None,
) -> None:
    """入队原始通知文本。priority: 'next' | 'later'，默认 later。"""
    prio = Priority.NEXT if priority == "next" else Priority.LATER
    item = PendingNotification(content, prio, recipient)
    with _lock:
        if prio == Priority.NEXT:
            _next_queue.append(item)
        else:
            _later_queue.append(item)


def enqueue_task_notification(
    status: str,
    summary: str,
    *,
    priority: str = "later",
) -> None:
    """入队结构化 task_notification XML（对齐 utils/task/framework.ts:267）。"""
    xml = (
        f"<task_notification>\n"
        f"  <status>{status}</status>\n"
        f"  <summary>{summary}</summary>\n"
        f"</task_notification>"
    )
    enqueue_pending_notification(xml, priority)


def consume_pending_notifications(*, recipient: str | None = None) -> list[str]:
    """
    消费点（对齐 query.ts:1566-1593）：drain 队列，next 优先于 later。
    recipient=None 只消费 Lead 全局通知；指定 agent 名则只消费该 agent 的后台任务通知。
    """
    with _lock:
        def _matches(item: PendingNotification) -> bool:
            if recipient is None:
                return item.recipient is None
            return item.recipient == recipient

        next_items = [i for i in _next_queue if _matches(i)]
        later_items = [i for i in _later_queue if _matches(i)]
        _next_queue[:] = [i for i in _next_queue if not _matches(i)]
        _later_queue[:] = [i for i in _later_queue if not _matches(i)]
    return [item.content for item in next_items] + [item.content for item in later_items]


def has_pending_notifications() -> bool:
    with _lock:
        return bool(_next_queue or _later_queue)
