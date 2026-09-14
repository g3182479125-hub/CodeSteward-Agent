"""Measure the in-process H2 route lifecycle without changing user state."""

from __future__ import annotations

import asyncio
import json
import math
import statistics
import time
from pathlib import Path

from nanobot.agent.plugins import PluginManifest, PluginRecord, PluginSource
from nanobot.agent.plugins.manager import PluginManager
from nanobot.agent.tools.registry import ToolRegistry

SAMPLES = 50


async def main() -> None:
    root = Path(__file__).parents[1] / "examples" / "pluggable-tool"
    manifest = PluginManifest.from_mapping(
        json.loads((root / "plugin-manifest.json").read_text(encoding="utf-8"))
    )
    manager = PluginManager(ToolRegistry())
    manager.register_version(
        PluginRecord(manifest, PluginSource.DEVELOPER, session_id="h10-benchmark"), root
    )
    manager.authorize("h10-benchmark")
    samples: list[float] = []

    for _ in range(SAMPLES):
        started = time.perf_counter_ns()
        loaded = manager.load(manifest.plugin_id, manifest.version, session_id="h10-benchmark")
        assert loaded.ok, loaded
        stopped = manager.stop(manifest.plugin_id, session_id="h10-benchmark")
        assert stopped.ok and stopped.operation_id, stopped
        status = await manager.wait_operation(
            stopped.operation_id, session_id="h10-benchmark", timeout=2
        )
        assert status["state"] == "inactive", status
        samples.append((time.perf_counter_ns() - started) / 1_000_000)

    ordered = sorted(samples)
    print(
        json.dumps(
            {
                "samples": SAMPLES,
                "p50_ms": round(statistics.median(samples), 3),
                "p95_ms": round(ordered[math.ceil(SAMPLES * 0.95) - 1], 3),
                "min_ms": round(ordered[0], 3),
                "max_ms": round(ordered[-1], 3),
                "residue": {
                    "active_routes": sum(len(routes) for routes in manager._routes.values()),
                    "retired_routes": len(manager._retired_routes),
                    "drain_tasks": len(manager._drain_tasks),
                },
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
