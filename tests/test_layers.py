"""constants.py and exceptions.py must stay free of imports of uvrev"""

import ast
from pathlib import Path

import pytest

import uvrev


@pytest.mark.parametrize("module", ["constants", "exceptions"])
def test_no_uvrev_imports(module: str) -> None:
    """Only imports within functions are allowed (e.g. terminal in exceptions)"""
    source = Path(uvrev.__file__).with_name(f"{module}.py").read_text()
    imports = [
        ast.unparse(node)
        for node in ast.parse(source).body
        if (
            isinstance(node, ast.ImportFrom)
            and (node.level or "uvrev" in str(node.module))
        )
        or (isinstance(node, ast.Import) and any("uvrev" in a.name for a in node.names))
    ]
    assert imports == []
