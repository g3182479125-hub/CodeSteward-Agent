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
6. Run `pnpm run browser-smoke`. This starts an ephemeral fake API and serves
   the production bundle so Edge/Chrome can load the actual React page against
   fixture responses. It checks page-body data for every console view plus
   Diff, timeline, and decision controls. Set `DEVPILOT_BROWSER` or
   `DEVPILOT_BROWSER_PROFILE_ROOT` when the local browser paths differ.

The automated browser smoke is dependency-free and does not require
Playwright. The in-app browser check on 2026-09-03 also completed successfully;
the standalone headless command may be affected by an already-running local
Edge/Chrome profile, in which case the in-app browser check is the fallback.
