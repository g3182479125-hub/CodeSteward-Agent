from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from devpilot.domain import FileOperation
from devpilot.patches import (
    AuthorizationGrant,
    FilePatch,
    PatchError,
    StructuredPatchService,
)
from devpilot.paths import AllowedRoots

FIXTURES = Path(__file__).parent / "fixtures"


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def copy_fixture(tmp_path: Path, name: str) -> Path:
    destination = tmp_path / name
    shutil.copytree(FIXTURES / name, destination)
    return destination


def apply_with_test_grant(service: StructuredPatchService, project: Path, patch: FilePatch):
    plan = service.plan(project, project_id=f"fixture-{project.name}", patches=(patch,))
    grant = AuthorizationGrant(
        change_set_id=plan.change_set.id,
        project_id=plan.project_id,
        patch_hash=plan.patch_hash,
        scope_hash=plan.scope_hash,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    return plan, service.apply(plan, grant)


@pytest.mark.parametrize(
    ("fixture_name", "relative_path", "old", "new"),
    [
        (
            "python-project",
            "src/demo/main.py",
            (
                b'def main() -> int:\n    return 0\n\n\n'
                b'if __name__ == "__main__":\n    raise SystemExit(main())\n'
            ),
            (
                b'def main() -> int:\n    return 1\n\n\n'
                b'if __name__ == "__main__":\n    raise SystemExit(main())\n'
            ),
        ),
        (
            "node-project",
            "src/index.js",
            b'export function main() {\n  return "ok";\n}\n',
            b'export function main() {\n  return "updated";\n}\n',
        ),
    ],
)
def test_python_and_node_fixtures_support_diff_apply_and_rollback(
    tmp_path: Path, fixture_name: str, relative_path: str, old: bytes, new: bytes
) -> None:
    project = copy_fixture(tmp_path, fixture_name)
    assert (project / relative_path).read_bytes() == old
    service = StructuredPatchService(allowed_roots=AllowedRoots.from_paths([tmp_path]))

    plan, receipt = apply_with_test_grant(
        service,
        project,
        FilePatch(relative_path, FileOperation.UPDATE, new.decode(), digest(old)),
    )

    assert "-" in plan.diff and "+" in plan.diff
    assert (project / relative_path).read_bytes() == new
    service.rollback(receipt, plan)
    assert (project / relative_path).read_bytes() == old


def test_fixture_safety_cases_do_not_write(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path, "python-project")
    service = StructuredPatchService(allowed_roots=AllowedRoots.from_paths([tmp_path]))
    unsafe = (
        FilePatch(".env", FileOperation.CREATE, "KEY=value\n"),
        FilePatch("../outside.py", FileOperation.CREATE, "escape\n"),
        FilePatch("src/demo/main.py", FileOperation.UPDATE, "changed\n", "0" * 64),
    )
    for patch in unsafe:
        with pytest.raises(PatchError):
            service.plan(project, patches=(patch,))
    assert (project / "src/demo/main.py").read_bytes().startswith(b"def main")
