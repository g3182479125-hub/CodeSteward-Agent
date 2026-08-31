# P0 architecture boundary

```text
User
  │ natural-language request
  ▼
nanobot WebUI / Gateway
  ▼
nanobot AgentLoop + AgentRunner
  ├─ Provider (DeepSeek)
  ├─ Skill Loader (published Skills only)
  └─ Tool Registry
       │ future, fixed-schema tools
       ▼
DevPilot Application Services
  ├─ Project / Task / Run / Event
  ├─ Skill validation and atomic publication
  ├─ Policy / Approval
  ├─ Execution Backend
  └─ Artifact / SQLite / observability
```

## Ownership

| Component | Owner | P0 decision |
|---|---|---|
| Runtime, Provider, base Session and Skill loading | nanobot | use upstream as-is |
| Project workflows and structured records | DevPilot | implement outside nanobot |
| Plugin composition and lifecycle ideas | DeepSeek Harness | reference only |
| Permission, approval, sandbox and event ideas | Codex Harness | reference only |
| Source code, Git and process actions | DevPilot Tools | fixed schemas and policy checks |

## Request flow

1. nanobot receives the user request.
2. A published Skill describes the allowed workflow and Tool set.
3. DevPilot checks Project identity, allowed roots and Policy before execution.
4. The Tool returns structured facts; it does not return an unbounded shell capability.
5. DevPilot appends Run Events and creates an Artifact when the workflow succeeds.

P0 does not implement the Tool or workflow steps. It only freezes this boundary.
