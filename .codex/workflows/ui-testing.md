# UI testing and debugging entry point

Use the Computer Use procedures maintained at
`$CODEX_HOME/tools/ui-testing-and-debugging/README.md`.

Before testing, read
`$CODEX_HOME/tools/ui-testing-and-debugging/docs/requirements.md`, resolve material
ambiguity, and pass the CLI requirements gate. Then follow the tool's `testing.md`
and `debugging.md` procedures. Use Computer Use for browser interaction and Chrome
DevTools for network requests, payloads, responses, and console inspection. Fix and
retest until scoped stability or a concrete blocker. Label supplementary automation
separately.

Keep UI run records and evidence under the shared tool directory. Keep generated
browser evidence outside tracked project paths unless the task explicitly requires
an artifact.
