# Changelog

Changes are recorded here using semantic versions.

## [0.1.0] - Unreleased

### Added

- `toucan-source-daymark`: `list`, `read`, `changes --after` and `write` over a Daymark project, implementing toucan's `toucan.plan/1` protocol. Requires `daymark-core` 0.4.0.
- Fields for stage, owner, start, end, deadline, priority, labels, dependencies, parent, milestone membership and marker, goal, type, done, estimate and every custom field a project declares (`field:ID`).
- Writes quote the item's version and are idempotent by mutation within a project; a mutation reused for a different change answers `conflict`.
- Read-only projects (`managed_by` set in Daymark) refuse writes with `forbidden`.
- Never creates or upgrades a database; a file at another Daymark schema answers `unavailable` and names the release it needs.
