"""The lowest modules must stay free of imports of other uvrev modules"""

import ast
from pathlib import Path

import pytest

import uvrev


def uvrev_imports(module: str) -> list[str]:
    """The uvrev modules imported at the top level of a module.

    Imports within functions and under ``TYPE_CHECKING`` don't count: they
    do not run when the module is imported.
    """
    source = Path(uvrev.__file__).with_name(f"{module}.py").read_text()
    found: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom) and node.level:
            found.extend([node.module] if node.module else [a.name for a in node.names])
        elif isinstance(node, ast.ImportFrom) and str(node.module).startswith("uvrev"):
            found.append(str(node.module).removeprefix("uvrev").lstrip("."))
        elif isinstance(node, ast.Import):
            found.extend(a.name for a in node.names if a.name.startswith("uvrev"))
    return sorted(found)


@pytest.mark.parametrize(
    ("module", "allowed"),
    [
        ("constants", []),
        ("exceptions", []),
        ("types", ["constants"]),
    ],
)
def test_allowed_uvrev_imports(module: str, allowed: list[str]) -> None:
    assert uvrev_imports(module) == allowed
