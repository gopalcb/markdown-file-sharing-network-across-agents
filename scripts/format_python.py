"""Format telemetry Python with Ruff and preserve blank lines between logical steps.

Use --check in CI. AST statement locations keep string literals, comprehensions
and multiline calls intact; this pass never rewrites executable Python tokens.
Packaged skills are excluded because their source has a separate approval boundary.
"""

import argparse
import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def separate_steps(source: str) -> str:
    """Insert blank lines before control flow and separate standalone call phases."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    boundaries = set()
    control = (
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.Try,
        ast.With,
        ast.AsyncWith,
        ast.Match,
    )

    for parent in ast.walk(tree):
        for _, value in ast.iter_fields(parent):
            if not isinstance(value, list):
                continue

            statements = [node for node in value if isinstance(node, ast.stmt)]

            for previous, current in zip(statements, statements[1:]):
                call = isinstance(current, ast.Expr) and isinstance(current.value, ast.Call)

                if not isinstance(current, control + (ast.Return,)) and not call:
                    continue

                start = current.lineno - 1

                # Keep a leading explanatory comment attached to its statement.
                while start > previous.end_lineno and lines[start - 1].lstrip().startswith('#'):
                    start -= 1

                if start > 0 and lines[start - 1].strip():
                    boundaries.add(start)

    return ''.join(('\n' if index in boundaries else '') + line for index, line in enumerate(lines))


def source_files() -> list[Path]:
    """Select maintained portal/tool Python, excluding environments and build artifacts."""
    files = []

    for directory in ('telemetry-api', 'aws-infrastructure', 'tests', 'scripts'):
        for path in (ROOT / directory).rglob('*.py'):
            relative = path.relative_to(ROOT)

            if any(
                part.startswith('.')
                or part in {'node_modules', 'build', 'dist', 'cdk.out', '__pycache__'}
                for part in relative.parts
            ):
                continue

            files.append(path)

    return sorted(files)


def main() -> None:
    """Apply or check imports, standard formatting and logical whitespace consistently."""
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    files = source_files()
    lint = [sys.executable, '-m', 'ruff', 'check', *map(str, files)]

    if not args.check:
        lint.append('--fix')

    subprocess.run(lint, cwd=ROOT, check=True)
    formatting = [sys.executable, '-m', 'ruff', 'format', *map(str, files)]

    if args.check:
        formatting.append('--check')

    subprocess.run(formatting, cwd=ROOT, check=True)
    changed = []

    for path in files:
        source = path.read_text()
        result = separate_steps(source)

        if result != source:
            changed.append(str(path.relative_to(ROOT)))

            if not args.check:
                path.write_text(result)

    if args.check and changed:
        raise SystemExit('Logical spacing required: ' + ', '.join(changed))

    print(f'Python formatting {"checked" if args.check else "applied"}: {len(files)} files')


if __name__ == '__main__':
    main()
