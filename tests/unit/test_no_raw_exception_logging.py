"""Guard: a caught exception is logged through ``describe_exception``, never raw.

An exception's message is built from whatever failed, and here that is often a
document or a model answer (a parse error quotes it). Logging ``exc`` / ``str(exc)``
/ ``f"{exc}"`` prints it into ``scratch/*.log``. Use
``app.core.safe_error.describe_exception(exc)`` (tracebacks via ``exc_info=True``
are sanitised by ``LocalTimeFormatter``).

The check is syntactic: it sees the exception variable used inside the logging call
itself. Assigning ``msg = str(exc)`` first and logging ``msg`` is not caught, so
don't.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

APP = Path(__file__).resolve().parents[2] / "app"
LEVELS = {"debug", "info", "warning", "error", "exception", "critical", "warn"}
RAW_ATTRS = {"args", "message", "msg"}


def _is_logger_call(node: ast.Call) -> bool:
    f = node.func
    if not (isinstance(f, ast.Attribute) and f.attr in LEVELS):
        return False
    recv = f.value
    name = recv.id if isinstance(recv, ast.Name) else getattr(recv, "attr", "")
    return "log" in name.lower()


def _raw_uses(node: ast.AST, name: str) -> list[ast.AST]:
    """Occurrences of ``name`` that would print the exception's message."""
    if isinstance(node, ast.Name):
        return [node] if node.id == name else []
    if isinstance(node, ast.Call):
        fn = node.func
        callee = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        if callee in {"describe_exception", "type", "isinstance"}:
            return []
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == name
    ):
        return [node] if node.attr in RAW_ATTRS else []
    hits: list[ast.AST] = []
    for child in ast.iter_child_nodes(node):
        hits += _raw_uses(child, name)
    return hits


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for handler in ast.walk(tree):
        if not (isinstance(handler, ast.ExceptHandler) and handler.name):
            continue
        for stmt in handler.body:
            for call in ast.walk(stmt):
                if isinstance(call, ast.Call) and _is_logger_call(call):
                    args = [*call.args, *(k.value for k in call.keywords)]
                    for arg in args:
                        for hit in _raw_uses(arg, handler.name):
                            found.append(f"{path.relative_to(APP.parent)}:{hit.lineno}")
    return sorted(set(found))


def test_no_caught_exception_is_logged_raw():
    offenders = [v for p in sorted(APP.rglob("*.py")) for v in _violations(p)]
    assert not offenders, (
        "log describe_exception(exc), not the exception itself:\n"
        + "\n".join(offenders)
    )
