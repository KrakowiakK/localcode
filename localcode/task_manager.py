"""Task management for planned, multi-step executions.

Minimal API to support plan -> per-task execution -> summary-only merge.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from localcode.middleware import logging_hook


@dataclass
class Task:
    task_id: str
    description: str
    status: str = "pending"  # pending | in_progress | completed | failed
    priority: Optional[str] = None
    summary: Optional[str] = None
    files_changed: List[str] = field(default_factory=list)
    files_read: List[str] = field(default_factory=list)


@dataclass
class TaskContext:
    task_id: str
    parent_summary: Optional[str] = None
    messages: List[Dict[str, Any]] = field(default_factory=list)

    def get_full_context(self) -> List[Dict[str, Any]]:
        ctx: List[Dict[str, Any]] = []
        if self.parent_summary:
            ctx.append({"role": "user", "content": f"<context>{self.parent_summary}</context>"})
        ctx.extend(self.messages)
        return ctx


class TaskManager:
    def __init__(self) -> None:
        self._tasks: Dict[str, Task] = {}
        self._order: List[str] = []
        self._auto_id = 1

    def _next_auto_id(self) -> str:
        while True:
            candidate = f"task_{self._auto_id}"
            self._auto_id += 1
            if candidate not in self._tasks:
                return candidate

    def create_tasks(self, tasks: List[Dict[str, Any]]) -> List[Task]:
        created: List[Task] = []
        for t in tasks or []:
            if isinstance(t, str):
                task_id = ""
                desc = t.strip()
            elif isinstance(t, dict):
                task_id = str(t.get("id") or t.get("task_id") or "").strip()
                desc = str(t.get("description") or t.get("content") or "").strip()
            else:
                continue
            if not desc:
                continue
            if not task_id:
                task_id = self._next_auto_id()
            if task_id in self._tasks:
                continue
            task = Task(
                task_id=task_id,
                description=desc,
                status=str(t.get("status") or "pending") if isinstance(t, dict) else "pending",
                priority=(str(t.get("priority")).strip() if isinstance(t, dict) and t.get("priority") is not None else None),
            )
            self._tasks[task_id] = task
            self._order.append(task_id)
            created.append(task)
        return created

    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> List[Task]:
        return [self._tasks[tid] for tid in self._order if tid in self._tasks]

    def has_tasks(self) -> bool:
        return bool(self._tasks)

    def update_task(self, task_id: str, **fields: Any) -> Optional[Task]:
        task = self._tasks.get(task_id)
        if not task:
            return None
        for key, value in fields.items():
            if hasattr(task, key):
                setattr(task, key, value)
        return task

    def format_tasks(self) -> str:
        lines: List[str] = []
        for task in self.list_tasks():
            status = task.status or "pending"
            lines.append(f"- [{status}] {task.task_id}: {task.description}")
        return "\n".join(lines)

    def start_task(self, task_id: str) -> Optional[Task]:
        task = self._tasks.get(task_id)
        if not task:
            return None
        task.status = "in_progress"
        logging_hook.log_event("task_start", {
            "task_id": task.task_id,
            "status": task.status,
            "files_changed": list(task.files_changed),
            "summary_len": len(task.summary or ""),
        })
        return task

    def end_task(
        self,
        task_id: str,
        status: str = "completed",
        summary: Optional[str] = None,
        files_changed: Optional[List[str]] = None,
        files_read: Optional[List[str]] = None,
        error: Optional[str] = None,
    ) -> Optional[Task]:
        task = self._tasks.get(task_id)
        if not task:
            return None
        task.status = status
        if summary is not None:
            task.summary = summary
        if files_changed is not None:
            task.files_changed = list(files_changed)
        if files_read is not None:
            task.files_read = list(files_read)
        payload = {
            "task_id": task.task_id,
            "status": task.status,
            "files_changed": list(task.files_changed),
            "files_read": list(task.files_read),
            "summary_len": len(task.summary or ""),
        }
        if error:
            payload["error"] = error
        logging_hook.log_event("task_end", payload)
        return task
