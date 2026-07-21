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
_DELEGATED_GOAL_MAX_CHARS = 80


class TodoChecklist:
    """Track one turn's authoritative task state and delegated child goals."""

    def __init__(self, delegated_tasks: str = "off") -> None:
        self._items: OrderedDict[str, dict[str, str]] = OrderedDict()
        self._delegated: list[str] = []
        self._delegated_tasks = (
            delegated_tasks if delegated_tasks in {"off", "count", "goal"} else "off"
        )

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
        rendered = self.render()
        # A valid empty result is an explicit clear signal. ``None`` remains
        # reserved for malformed/error results that must not disturb the last
        # known-good display state.
        return rendered if rendered is not None else ""

    def add_delegations(self, args: Any) -> str | None:
        """Append goals dispatched through ``delegate_task`` to this turn."""
        if self._delegated_tasks == "off":
            return self.render()
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
        """Return visible task/delegation sections, or ``None`` when empty."""
        if not self._items and not self._delegated:
            return None

        lines: list[str] = []
        if self._items:
            active = [item for item in self._items.values() if item["status"] != "completed"]
            completed = [item for item in self._items.values() if item["status"] == "completed"]
            count = len(active)
            noun = "task" if count == 1 else "tasks"
            if not active:
                heading = "All tasks complete:"
            elif completed:
                heading = f"Working on {count} remaining {noun}:"
            else:
                heading = f"Working on {count} {noun}:"
            lines = [heading, ""]
            lines.extend(
                f"{_STATUS_ICONS[item['status']]} {item['content']}"
                for item in (*active, *completed)
            )

        if self._delegated:
            if lines:
                lines.append("")
            if self._delegated_tasks == "count":
                count = len(self._delegated)
                noun = "task" if count == 1 else "tasks"
                lines.append(f"Delegated {count} {noun} 🤖")
            elif self._delegated_tasks == "goal":
                lines.append("🤖 Delegated tasks")
                lines.extend(f"↳ {_truncate_goal(goal)}" for goal in self._delegated)
        return "\n".join(lines)


def _truncate_goal(goal: str) -> str:
    """Cap displayed delegated goals without changing the child prompt."""
    if len(goal) <= _DELEGATED_GOAL_MAX_CHARS:
        return goal
    return goal[: _DELEGATED_GOAL_MAX_CHARS - 1] + "…"
