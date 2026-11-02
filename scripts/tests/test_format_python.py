"""Protect formatter idempotence and string/statement semantics."""

import ast
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    'format_python', Path(__file__).resolve().parents[1] / 'format_python.py'
)
formatter = importlib.util.module_from_spec(SPEC)

SPEC.loader.exec_module(formatter)


def test_logical_spacing_preserves_syntax_and_strings() -> None:
    """Separate control/call phases without changing a multiline literal or Python AST."""
    source = '''def work(items):
    text = """literal
if not code:
    print('not code')
"""
    # Explain the loop.
    for item in items:
        result = item + 1
        if result:
            print(result)
            save(result)
    return text
'''
    result = formatter.separate_steps(source)
    assert ast.dump(ast.parse(source)) == ast.dump(ast.parse(result))
    assert '\n\n    # Explain the loop.\n    for' in result
    assert '\n\n        if result:' in result
    assert '\n\n            save(result)' in result
    assert 'literal\nif not code:' in result
    assert formatter.separate_steps(result) == result
