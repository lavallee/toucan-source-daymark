# Working on toucan-source-daymark

A small adapter: toucan runs it as `toucan-source-daymark list|read|changes|write`
and it reads and writes a Daymark project. Keep it small and boring.

- Develop against a local Daymark (see README, Development); `uv run --no-sync
  pytest -q`, `ruff check src tests`, `ruff format --check src tests`.
- The wire format is toucan's `toucan.plan/1`; toucan's docs are canonical. Do
  not change a wire shape without changing toucan's side to match.
- Use Daymark's public API (`Store`, `daymark.plans`); add what is missing to
  Daymark rather than reaching into its private helpers.
- Never create, upgrade or delete a database. Never write to a project with
  `managed_by` set.
- Tests run the installed console script as a subprocess against temporary
  databases. Synthetic records only.
