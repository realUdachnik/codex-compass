---
name: codex-compass
description: Improve unfamiliar or multi-file coding work with grounded discovery, proportionate solution choice, consequence checks, and bounded autonomous correction. Use the optional controller when explicit acceptance and retries help.
---

Resolve the user's practical outcome, relevant existing behavior, and constraints from the request and repository. Do not ask the user to enumerate likely mistakes. Scale effort to uncertainty and consequence: a straightforward local edit does not need a design exercise, agent debate, or controller.

For an unfamiliar, nontrivial change, obtain missing repository facts cheaply. Use [scripts/compass.py](scripts/compass.py) with Python, or `codex-compass context --workspace <repo> --goal '...' --max-chars 6000`, unless equivalent current facts are already known. Choose relevant files yourself; use `--seed path/to/file` for focused references. Read the implementation, its callers, entrypoints, configuration, tests, and an existing analogous solution as relevant. Reuse existing conventions when they fit the requested outcome.

The map is incomplete evidence, not a runtime dependency graph. Read actual files before acting; a text match does not prove a caller. Use coverage and truncation to target further reading. Avoid repeated broad scans or unchanged context dumps.

When the choice matters, briefly compare plausible approaches against the actual constraints: correctness, existing integration, failure behavior, effort, and ongoing cost. Prefer the least complicated approach that satisfies the important requirements; the shortest patch is not always the cheapest complete solution. Resolve a material uncertainty with a focused experiment rather than extended speculation. Do not invent alternatives merely to fill a quota.

Before editing, identify likely costly failures and affected user paths: for example partial writes, repeated requests, concurrent changes, invalid inputs, or lost state, when relevant. Trace consequences through real callers. Choose a few checks that can disprove the approach, including requested behavior and an important old case or failure boundary. Do not expand scope into hypothetical features.

Implement, then exercise those paths. Review the result against the user's outcome and observed evidence, including assumptions made earlier. Fix failures with the largest practical impact first. If a failure repeats, change the hypothesis or decomposition instead of repeating unchanged work or weakening checks. Continue autonomously through authorized correction; stop polishing when important requirements pass and no concrete high-impact issue remains. Extra tests and longer explanations are not progress by themselves.

Carry existing authorization through ordinary inspection, reversible implementation, testing, and requested-service access. Read available configuration before asking for known information; keep credentials out of incidental logs. A failed access path is not proof the source is unavailable: try another documented, authorized path when useful. Ask only for a consequential missing choice or required approval, while continuing independent work. Do not invent missing facts or results.

Use the optional bounded controller only when executable acceptance and retries justify it; then read [references/controller.md](references/controller.md). Ordinary small work can be completed directly. Respect the current invocation's limits on delegation and model calls.

For explicitly requested monitored long work, [references/reminders.md](references/reminders.md) describes the optional native reminder launcher. Ordinary skill text alone does not install lifecycle hooks. When monitoring is already configured, treat reminders as bounded observations to check against current evidence; do not restart monitoring or assume they prove a mistake.

Report the outcome, relevant checks, and material limitations plainly and briefly. Passing named checks is evidence for those checks, not proof of every requirement. Compass does not upgrade the model, expand permission, authorize new external communications, or override higher-priority instructions.
