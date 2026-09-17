# Agent repair challenge: local task board

This directory is an isolated training copy derived from
[pamelafox/python-project-template](https://github.com/pamelafox/python-project-template),
which is MIT-licensed. The upstream repository and its history are preserved;
this local branch is intentionally modified for an Agent repair exercise.

## User story

Please finish the small JSON-backed task-board CLI in `main.py`. A user should
be able to add, list, complete, and remove tasks without losing the identity of
existing tasks. The database path is selected with `--db` and should work when
its parent directory does not exist yet.

Keep the original `add_numbers` example working. Use the existing pytest setup,
add or improve tests where useful, and keep the implementation dependency-free
beyond the Python standard library.

## Acceptance

Run these commands from this directory:

```powershell
python -m pytest -q
python main.py --db .\demo\tasks.json add "write release notes"
python main.py --db .\demo\tasks.json list
python main.py --db .\demo\tasks.json complete 1
python main.py --db .\demo\tasks.json remove 1
```

The test suite must pass. IDs must remain unique and stable after removals,
unknown IDs must produce a clear handled error, and an empty title must be
rejected without corrupting the JSON database.

Do not use network services, external credentials, or files outside this
directory. Do not modify the parent DevPilot/nanobot repository while solving
this challenge.
