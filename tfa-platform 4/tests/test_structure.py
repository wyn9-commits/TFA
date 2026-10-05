"""The layering rule, enforced.

A folder structure is a comment unless something checks it. These tests fail
the build when a dependency points the wrong way — which is how the previous
layout drifted into holding pure rules, an Azure adapter and orchestration in
one `extraction/` folder.

    domain    <- depends on nothing in this package
    pipeline  -> domain only
    adapters  -> domain only
"""
from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "tfa_core"

VENDOR_ROOTS = {
    "azure", "httpx", "requests", "pyodbc", "pymssql", "sqlalchemy",
    "fastapi", "starlette", "flask", "openai", "langgraph", "langsmith",
    "fitz", "pypdfium2",
}


def imports_of(path: pathlib.Path, top_level_only: bool = False) -> set[str]:
    """Collects imported module names.

    `top_level_only` is the distinction that matters for layering. A
    module-level import forces its dependency at import time; an import inside
    a function is a deliberate lazy resolve, which is how an optional vendor
    (a tracer, a graph engine) stays optional. Treating the two the same would
    push the codebase toward either eager coupling or dead abstraction.
    """
    tree = ast.parse(path.read_text())
    nodes = tree.body if top_level_only else list(ast.walk(tree))
    found: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
    return found


def modules(package: str) -> list[pathlib.Path]:
    return [p for p in (SRC / package).rglob("*.py") if p.name != "__init__.py"]


def test_domain_imports_no_vendor_sdk():
    """The rules that decide a monetary figure must be readable, reviewable and
    testable without an Azure account."""
    offenders = []
    for path in modules("domain"):
        for name in imports_of(path):
            if name.split(".")[0] in VENDOR_ROOTS:
                offenders.append(f"{path.name} imports {name}")
    assert not offenders, "domain must stay pure:\n  " + "\n  ".join(offenders)


def test_domain_does_not_depend_on_other_layers():
    offenders = []
    for path in modules("domain"):
        for name in imports_of(path):
            if name.startswith(("tfa_core.adapters", "tfa_core.pipeline",
                                "tfa_core.config")):
                offenders.append(f"{path.name} imports {name}")
    assert not offenders, "domain must not look outward:\n  " + "\n  ".join(offenders)


def test_pipeline_does_not_import_adapters():
    """Orchestration talks to protocols in domain.ports, not to vendors. That
    is what lets the ingestion path run against fakes with no credentials."""
    offenders = []
    for path in modules("pipeline"):
        for name in imports_of(path, top_level_only=True):
            if name.startswith("tfa_core.adapters"):
                offenders.append(f"{path.name} imports {name} at module level")
    assert not offenders, (
        "pipeline must reach vendors through ports:\n  " + "\n  ".join(offenders))


def test_adapters_do_not_import_each_other():
    """One module per external thing. Cross-imports turn a vendor swap into a
    cascade."""
    # Shared transport concerns, and an adapter using the connection engine
    # for its own vendor (sql_store -> sql_engine), are legitimate. What is
    # not: one vendor's adapter reaching into another's.
    allowed = {"tfa_core.adapters.resilience", "tfa_core.adapters.sql_engine",
               "tfa_core.adapters.excel_reader", "tfa_core.adapters.csv_reader"}
    offenders = []
    for path in modules("adapters"):
        for name in imports_of(path):
            if (name.startswith("tfa_core.adapters")
                    and name not in allowed
                    and not name.endswith(path.stem)):
                offenders.append(f"{path.name} imports {name}")
    assert not offenders, "adapters should be independent:\n  " + "\n  ".join(offenders)


def test_every_module_sits_in_a_layer():
    """No root-level sprawl. The previous layout had ten files at the package
    root with no stated relationship to each other."""
    # config.py and container.py sit at the root by design: settings and the
    # composition root belong to no layer, and the container must be able to
    # import from all of them.
    loose = [p.name for p in SRC.glob("*.py")
             if p.name not in ("__init__.py", "config.py", "container.py")]
    assert not loose, f"these belong in a layer: {loose}"


def test_money_rules_are_importable_alone():
    """The practical form of the rule above: reconciliation and validation
    must import with nothing but the standard library and pydantic."""
    for name in ("validation", "reconciliation", "identity", "rules", "columns"):
        path = SRC / "domain" / f"{name}.py"
        assert path.exists(), f"domain/{name}.py missing"
        for imported in imports_of(path):
            root = imported.split(".")[0]
            assert root not in VENDOR_ROOTS, f"domain/{name}.py imports {imported}"
