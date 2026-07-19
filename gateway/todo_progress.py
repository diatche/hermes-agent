"""Live task-checklist presentation for messaging gateways.

``TodoChecklist`` renders authoritative full todo-tool results and appends goals
launched through ``delegate_task``. Delivery remains owned by the gateway; this
module performs no I/O and never mutates conversation history.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any, Mapping


_STATUS_ICONS = {
    "pending": "⬜",
    "in_progress": "🔄",
    "completed": "✅",
    "cancelled": "🚫",
}


class TodoChecklist:
    """Track one turn's authoritative task state and delegated child goals."""

    def __init__(self) -> None:
        self._items: OrderedDict[str, dict[str, str]] = OrderedDict()
        self._delegated: list[str] = []
        self._has_todo_state = False

    def update_from_result(self, result: Any) -> str | None:
        """Replace task state from a successful todo tool's full JSON result.

        TodoStore applies replace/merge validation before producing this result,
        so consuming the full result avoids maintaining a divergent shadow
        implementation in the gateway. Malformed/error results are ignored.
        """
        if isinstance(result, str):
            try:
                payload = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                return None
        elif isinstance(result, Mapping):
            payload = result
        else:
            return None

        raw_items = payload.get("todos")
        if not isinstance(raw_items, list):
            return None

        items: OrderedDict[str, dict[str, str]] = OrderedDict()
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                return None
            item_id = str(raw.get("id") or "").strip()
            content = str(raw.get("content") or "").strip()
            status = str(raw.get("status") or "").strip().lower()
            if not item_id or not content or status not in _STATUS_ICONS:
                return None
            items[item_id] = {"id": item_id, "content": content, "status": status}

        self._items = items
        self._has_todo_state = True
        return self.render()

    def add_delegations(self, args: Any) -> str | None:
        """Append goals dispatched through ``delegate_task`` to this turn."""
        if not isinstance(args, Mapping):
            return self.render()

        goals: list[str] = []
        raw_tasks = args.get("tasks")
        if isinstance(raw_tasks, list):
            for raw in raw_tasks:
                if isinstance(raw, Mapping):
                    goal = str(raw.get("goal") or "").strip()
                    if goal:
                        goals.append(goal)
        else:
            goal = str(args.get("goal") or "").strip()
            if goal:
                goals.append(goal)

        for goal in goals:
            if goal not in self._delegated:
                self._delegated.append(goal)
        return self.render()

    def render(self) -> str | None:
        """Return current tasks plus delegated goals, or ``None`` before either."""
        if not self._has_todo_state and not self._delegated:
            return None

        completed = sum(item["status"] == "completed" for item in self._items.values())
        lines = [f"📋 Tasks — {completed}/{len(self._items)} completed", ""]
        if self._items:
            lines.extend(
                f"{_STATUS_ICONS[item['status']]} {item['content']}"
                for item in self._items.values()
            )
        elif self._has_todo_state:
            lines.append("No tasks")

        if self._delegated:
            if self._items or self._has_todo_state:
                lines.append("")
            lines.append("🤖 Delegated tasks")
            lines.extend(f"↳ {goal}" for goal in self._delegated)
        return "\n".join(lines)
