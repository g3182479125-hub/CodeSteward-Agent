# Spec review: Bug-fix analysis-evidence reuse

- Fixed base: `0d8cf67`
- Initial candidate: `075846d`
- Incremental candidate: `0a4ef50`
- Reviewer: Spec subagent `01a0acf6-963c-7bf3-acb4-4d90cdcf6506`

## Initial review

**REQUEST_CHANGES (P1).** The initial fallback scanned only the newest 32
same-project analysis runs. That did not meet the handoff contract: a valid
complete analysis can be older than many incomplete runs.

## Resolution and incremental review

`0a4ef50` removes the cap and covers the older-complete/33-newer-incomplete
case. The Spec reviewer returned **PASS**: fallback remains same-project only;
unknown, cross-project and forged evidence still reject; no model-made evidence
or execution/confirmation/hash guard was relaxed.

