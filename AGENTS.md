## Coding Mode
Treat every task as a job to complete, not a lesson.

## Working Style
- Fix or build directly — don't explain your reasoning before acting unless I ask
- Just make the change; skip the walkthrough of which files you looked at and why
- Don't ask "what do you think is causing this?" — give the answer
- Run commands as needed without waiting for me to say "go ahead"
- Don't narrate — skip "I will proceed to..." and "Let me now..."
- After fixing, one line on the root cause is fine; skip the "how to spot it next time" teaching

## Codebase Conventions
- Match the existing codebase style — naming, formatting, file structure, patterns
- Check how similar things are done nearby and follow that, don't impose your own conventions
- Use the libraries and utilities already in the project; don't add new dependencies without asking
- Match existing import style, error handling, and typing conventions

## Explanations
- Keep them brief and only where they matter (non-obvious decisions, tradeoffs)
- Don't explain standard library or built-in functions I'd already know
- Only flag a custom function's location if it's relevant to the change

## Output
- Concise. Answer first, minimal preamble.
- No teaching scaffolding, no step-by-step reveals

## Agent skills

### Issue tracker

Issues live as markdown files under `.scratch/<feature-slug>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles, written verbatim on each issue file's `Status:` line. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` at the root, ADRs under `docs/adr/`. See `docs/agents/domain.md`.
