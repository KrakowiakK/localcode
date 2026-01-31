"""Tests for TaskManager and task logging."""

from localcode.task_manager import TaskManager
from localcode.middleware import logging_hook


def test_create_and_list_tasks():
    tm = TaskManager()
    created = tm.create_tasks([
        {"id": "t1", "description": "do A"},
        {"id": "t2", "description": "do B"},
    ])
    assert len(created) == 2
    tasks = tm.list_tasks()
    assert [t.task_id for t in tasks] == ["t1", "t2"]
    assert tasks[0].description == "do A"


def test_update_task_fields():
    tm = TaskManager()
    tm.create_tasks([{"id": "t1", "description": "do A"}])
    tm.update_task("t1", status="in_progress", summary="started", files_changed=["a.py"])
    task = tm.get_task("t1")
    assert task is not None
    assert task.status == "in_progress"
    assert task.summary == "started"
    assert task.files_changed == ["a.py"]


def test_create_tasks_auto_id_and_string():
    tm = TaskManager()
    created = tm.create_tasks([{"description": "do A"}, "do B", {"id": "", "description": "do C"}])
    assert len(created) == 3
    ids = [t.task_id for t in tm.list_tasks()]
    assert ids == ["task_1", "task_2", "task_3"]
    assert [t.description for t in tm.list_tasks()] == ["do A", "do B", "do C"]


def test_task_start_end_logs(monkeypatch):
    events = []

    def fake_log_event(event_type, payload=None):
        events.append((event_type, payload or {}))

    monkeypatch.setattr(logging_hook, "log_event", fake_log_event)

    tm = TaskManager()
    tm.create_tasks([{"id": "t1", "description": "do A"}])
    tm.start_task("t1")
    tm.end_task("t1", status="completed", summary="done", files_changed=["a.py"])

    assert events[0][0] == "task_start"
    assert events[1][0] == "task_end"
    assert events[1][1]["files_changed"] == ["a.py"]
    assert events[1][1]["summary_len"] == len("done")
