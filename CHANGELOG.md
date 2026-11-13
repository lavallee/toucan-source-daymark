# Changelog

Changes are recorded here using semantic versions.

## [0.1.0] - Unreleased

### Added

- `toucan-source-daymark`: `list`, `read`, `changes --after` and `write` over a Daymark project, implementing toucan's `toucan.plan/1` protocol. Requires `daymark-core` 0.4.0.
- Fields for stage, owner, start, end, deadline, priority, labels, dependencies, parent, the `milestone` marker, `inMilestone` membership, a read-only `container` flag, goal, type, done, estimate and every custom field a project declares (`field:ID`).
- `--` before a reference; a reference beginning with `-` is refused.
- Only items of the plan can be written, and `dependsOn` and `parent` take items of the plan only. A change that changes nothing writes nothing. A value that no longer fits its field is sent as `null` and listed in the field's `omitted`.
- Waits at most 5 s (`changes`) or 10 s per transaction for a busy database, stops starting new changes after 20 s, and answers `unavailable` with a plain reason; an older `daymark-core` is named rather than failing with a traceback.
- Writes quote the item's version and are idempotent by mutation within a project; a mutation reused for a different change answers `conflict`.
- Read-only projects (`managed_by` set in Daymark) refuse writes with `forbidden`.
- Never creates or upgrades a database; a file at another Daymark schema answers `unavailable` and names the release it needs.
