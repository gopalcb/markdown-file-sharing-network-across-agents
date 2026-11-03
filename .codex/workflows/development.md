# Development workflow

Use this workflow for complex, consequential, or explicitly planned work. Handle
questions and small low-risk changes directly. Keep the process proportional.

## 1. Plan

Load `plan-development` from the shared `$CODEX_HOME`. Inspect applicable
instructions, current source, tests, configuration, and relevant documentation.
Define scope, ordered tasks, acceptance criteria, risks, and the smallest useful
verification. For UI work, identify only the affected flows and viewports.

## 2. Save the plan document

Create or reuse a concise project task record and save the reviewed plan. Include
scope, tasks, acceptance criteria, verification, assumptions, and blockers. For a
planning-only request, stop here.

## 3. Implement

Load `implement-development` from the shared `$CODEX_HOME` and implement in
dependency order. Preserve unrelated changes and update only affected
documentation. Record minor deviations; revise the plan before making a material
scope or design change.

## 4. Test and validate

Inspect the final diff and run the smallest checks that prove the acceptance
criteria. Fix introduced failures and rerun only affected checks. For UI changes,
follow `.codex/workflows/ui-testing.md` once after the final relevant edit; do not
repeat unchanged browser checks. Mark blocked or unrun checks accurately.
Publishing, deployment, and live verification require explicit scope.

## 5. Save the work summary

Save a concise summary with completed tasks, changed files, deviations, acceptance
evidence, checks run, unverified behavior, blockers, and next steps. Report the
outcome and limitations to the user.
