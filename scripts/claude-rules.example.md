# Organization engineering rules (managed)

These rules are set by the platform team and pushed by the Claude gateway
to every developer. They load ahead of your own `~/.claude/CLAUDE.md` and
each project's `CLAUDE.md`, which add to them.

## Security

- Never hardcode credentials, tokens, or other secrets in code, config, or
  docs; use the approved secret store and reference secrets by name.
- Never pass a secret value on a command line — it is visible to other
  processes via `ps`. Use a hidden prompt or a mode-600 temp file, and never
  echo or persist secret values.
- Create key material and other sensitive files with restrictive permissions
  from the start (`umask 077`), never world-readable-then-chmod.
- Least privilege by default: scope IAM policies, database grants, and
  network rules to the exact resources needed. Broad access (wildcards,
  admin roles) needs an explicit, recorded justification.
- Do not paste internal source, logs, or data into external services.

## Process

- Run the project's test suite before committing and keep it green; fix or
  revert a red suite before starting new work. New behavior ships with a
  test, and a test that cannot fail is not a test.
- Verify every factual claim — names, defaults, retention periods, service
  behavior — against code or an authoritative source before writing it into
  docs or comments. If it cannot be verified, say so in the doc instead of
  writing a plausible answer.
- Report outcomes honestly: failing tests, skipped steps, and assumptions
  are stated, not smoothed over. "Done" means verified working, not
  "should work" — flag anything confirmed only from docs as needing a real
  run.

## Code

- Match the conventions, naming, and comment density of the code you are
  editing; reuse the project's existing helpers rather than re-implementing
  them.
- Write comments for constraints the code cannot show, not to narrate what
  the next line does.
- Before a destructive or hard-to-reverse action (deleting data, rewriting
  history, changing shared infrastructure), stop and confirm the target is
  what you think it is.
