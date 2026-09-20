"""A small JSON-backed task board used for the coding-agent exercise."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path


def add_numbers(a, b):
    """Keep the template's original example function intact."""
    return a + b


@dataclass
class Task:
    """One task stored by the local task board."""

    id: int
    title: str
    done: bool = False


def load_tasks(path: Path) -> list[Task]:
    """Load tasks from *path*, treating a missing file as an empty board."""
    if not path.exists():
        return []
    raw_tasks = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_tasks, list):
        raise ValueError("task database must contain a JSON list")
    return [
        Task(id=int(raw["id"]), title=str(raw["title"]), done=bool(raw["done"]))
        for raw in raw_tasks
    ]


def save_tasks(path: Path, tasks: list[Task]) -> None:
    """Persist tasks as readable JSON, creating the parent directory first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(task) for task in tasks], indent=2) + "\n",
        encoding="utf-8",
    )


def add_task(path: Path, title: str) -> Task:
    """Add a pending task and return it."""
    title = title.strip()
    if not title:
        raise ValueError("task title must not be empty")
    tasks = load_tasks(path)
    task = Task(id=max((item.id for item in tasks), default=0) + 1, title=title)
    tasks.append(task)
    save_tasks(path, tasks)
    return task


def complete_task(path: Path, task_id: int) -> Task:
    """Mark a task complete and return it."""
    tasks = load_tasks(path)
    task = next((item for item in tasks if item.id == task_id), None)
    if task is None:
        raise KeyError(f"task id {task_id}")
    task.done = True
    save_tasks(path, tasks)
    return task


def remove_task(path: Path, task_id: int) -> Task:
    """Remove a task by its stable ID and return the removed task."""
    tasks = load_tasks(path)
    for index, item in enumerate(tasks):
        if item.id == task_id:
            task = tasks.pop(index)
            break
    else:
        raise KeyError(f"task id {task_id}")
    save_tasks(path, tasks)
    return task


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage a local task board")
    parser.add_argument("--db", type=Path, default=Path("tasks.json"))
    commands = parser.add_subparsers(dest="command", required=True)

    add_parser = commands.add_parser("add", help="add a task")
    add_parser.add_argument("title")

    commands.add_parser("list", help="list tasks")

    complete_parser = commands.add_parser("complete", help="complete a task")
    complete_parser.add_argument("task_id", type=int)

    remove_parser = commands.add_parser("remove", help="remove a task")
    remove_parser.add_argument("task_id", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the task-board command line interface."""
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "add":
            task = add_task(args.db, args.title)
            print(f"Added #{task.id}: {task.title}")
        elif args.command == "list":
            for task in load_tasks(args.db):
                marker = "x" if task.done else " "
                print(f"{task.id}. [{marker}] {task.title}")
        elif args.command == "complete":
            task = complete_task(args.db, args.task_id)
            print(f"Completed #{task.id}: {task.title}")
        elif args.command == "remove":
            task = remove_task(args.db, args.task_id)
            print(f"Removed #{task.id}: {task.title}")
    except (KeyError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
