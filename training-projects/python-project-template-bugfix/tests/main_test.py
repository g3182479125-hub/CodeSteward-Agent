import pytest

from main import add_numbers, add_task, complete_task, load_tasks, remove_task


def test_add_numbers():
    assert add_numbers(2, 2) == 4


def test_add_and_complete_task(tmp_path):
    path = tmp_path / "tasks.json"

    created = add_task(path, "write acceptance test")
    completed = complete_task(path, created.id)

    assert completed.done is True
    assert load_tasks(path)[0].done is True


def test_add_uses_a_new_id_after_removing_a_task(tmp_path):
    path = tmp_path / "tasks.json"
    add_task(path, "first")
    add_task(path, "second")
    remove_task(path, 1)

    created = add_task(path, "third")

    assert created.id == 3


def test_remove_uses_stable_task_id(tmp_path):
    path = tmp_path / "tasks.json"
    add_task(path, "first")
    add_task(path, "second")
    add_task(path, "third")
    remove_task(path, 2)

    removed = remove_task(path, 3)

    assert removed.title == "third"
    assert [task.title for task in load_tasks(path)] == ["first"]


def test_complete_unknown_id_has_a_clear_error(tmp_path):
    path = tmp_path / "tasks.json"
    add_task(path, "known")

    with pytest.raises(KeyError, match="task id 99"):
        complete_task(path, 99)
