# P5 Browser Smoke Evidence

Date: 2026-09-03

The local API and Vite console were started on loopback ports `18800` and
`18801`. The browser smoke path was exercised against the running console:

1. Open `http://127.0.0.1:18801/` and confirm the Dashboard renders with API
   status.
2. Navigate through Projects, ChangeSet / Diff, Run Timeline, Artifacts,
   Skills, and Security / Backends.
3. Confirm the Skills catalog renders the discovered local skills and the
   Security page labels the unverified execution backend as unavailable.
4. Confirm ChangeSet Diff is collapsible and the delivery decision controls
   are inside the ChangeSet view; no separate approval-only page is created.
5. Run `pnpm run smoke` from this directory. The script verifies the console
   shell, API health, and Skills JSON boundary without storing credentials.

The automated smoke script is intentionally dependency-free. Interactive
browser checks remain a manual local step because Playwright is not a runtime
dependency of this small optional console.
