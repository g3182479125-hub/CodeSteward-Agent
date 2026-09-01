# P2B-01 durable Preview/ChangeSet acceptance

Date: 2026-09-01

`ControlPlaneManager.preview` now persists the immutable ChangeSet, complete structured patch
bodies, pre-apply snapshots, hashes and Diff material through the P2A journal. A new manager
instance reconstructs the same preview and can continue the lifecycle after process restart.
Preview includes project identity, plan version, risk summary, verification checks, stop conditions,
file summaries and Diff. No approval token is created by preview.

P2B-01 is accepted by the restart reconstruction and persistence tests. The proposal remains
bounded by the P2A path, secret, symlink, UTF-8, file, byte and changed-line limits.
