# Repository tooling

`codex.sh` launches the shared local Codex environment. It does not copy private
runtime state into this repository.

`format_python.py` runs pinned Ruff import/format checks and then inserts logical
blank lines using Python statement positions. `format-web.mjs` uses the TypeScript
parser to expand control bodies, Prettier for standard layout, and statement
positions for blank lines. Both support `--check` without writing files.

`tests/` checks that formatting preserves literals and control flow and is
idempotent. The formatter scripts define their deliberately limited scopes. No
formatter runs AWS operations or changes runtime settings.
