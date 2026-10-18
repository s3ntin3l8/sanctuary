"""Guard: a caught exception is logged through ``describe_exception``, never raw.

An exception's message is built from whatever failed, and here that is often a
document or a model answer (a parse error quotes it). Logging ``exc`` / ``str(exc)``
/ ``f"{exc}"`` prints it into ``scratch/*.log``. Use
``app.core.safe_error.describe_exception(exc)`` (tracebacks via ``exc_info=True``
are sanitised by ``LocalTimeFormatter``).

The check is syntactic: it sees the exception variable used inside the logging call
itself. The variable is an ``except ... as NAME`` target, a function parameter named
like an exception (``exc``, ``e``, ``error``, ...; a string parameter of that name is
flagged too: rename it) or a name assigned from one in the same function
(``last_error = e``, ``msg = str(exc)``). Not tracked: values that cross a function
boundary other than as a parameter, and ``getattr``/``setattr`` indirection.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

APP = Path(__file__).resolve().parents[2] / "app"
LEVELS = {"debug", "info", "warning", "error", "exception", "critical", "warn"}
RAW_ATTRS = {"args", "message", "msg", "detail"}
TRACEBACK_KWARGS = {"exc_info", "stack_info", "stacklevel"}
EXCEPTION_PARAMS = {"e", "ex", "exc", "err", "error", "exception"}


def _is_logger_call(node: ast.Call) -> bool:
    f = node.func
    if not (isinstance(f, ast.Attribute) and f.attr in LEVELS):
        return False
    recv = f.value
    if isinstance(recv, ast.Call):  # logging.getLogger(__name__).error(...)
        recv = recv.func
    name = recv.id if isinstance(recv, ast.Name) else getattr(recv, "attr", "")
    return "log" in name.lower()


def _raw_uses(node: ast.AST, name: str) -> list[ast.AST]:
    """Occurrences of ``name`` that would print the exception's message."""
    if isinstance(node, ast.Compare):  # `exc is None` prints nothing
        return []
    if isinstance(node, ast.IfExp):  # the condition is not what gets printed
        return _raw_uses(node.body, name) + _raw_uses(node.orelse, name)
    if isinstance(node, ast.Name):
        return [node] if node.id == name and isinstance(node.ctx, ast.Load) else []
    if isinstance(node, ast.Call):
        fn = node.func
        callee = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        if callee in {"describe_exception", "type", "isinstance", "hasattr"}:
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


def _assigned_from(func: ast.AST, tainted: set[str]) -> set[str]:
    """Names assigned (anywhere in ``func``) from an expression that uses a
    tainted name, e.g. ``last_error = e`` or ``msg = str(exc)``; repeated until
    nothing new turns up."""
    derived: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(func):
            if not isinstance(node, ast.Assign | ast.AnnAssign) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {t.id for t in targets if isinstance(t, ast.Name)}
            if names <= derived | tainted:
                continue
            if any(_raw_uses(node.value, n) for n in tainted | derived):
                derived |= names
                changed = True
    return derived


def scopes(tree: ast.AST):
    """``(variable, statements)``: where an exception variable is in scope."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.name:
            yield node.name, node.body
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            params = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            seeds = {p.arg for p in params if p.arg in EXCEPTION_PARAMS}
            seeds |= {
                h.name
                for h in ast.walk(node)
                if isinstance(h, ast.ExceptHandler) and h.name
            }
            for name in seeds & {p.arg for p in params}:
                yield name, node.body
            for name in _assigned_from(node, seeds):
                yield name, node.body


def logged_raw(tree: ast.AST):
    """``(variable, node)`` for every raw use of an exception in a logging call."""
    for name, body in scopes(tree):
        for stmt in body:
            for call in ast.walk(stmt):
                if isinstance(call, ast.Call) and _is_logger_call(call):
                    args = [
                        *call.args,
                        *(
                            k.value
                            for k in call.keywords
                            if k.arg not in TRACEBACK_KWARGS
                        ),
                    ]
                    for arg in args:
                        for hit in _raw_uses(arg, name):
                            yield name, hit


def violations_in(source: str) -> list[int]:
    return sorted({hit.lineno for _n, hit in logged_raw(ast.parse(source))})


def _violations(path: Path) -> list[str]:
    rel = path.relative_to(APP.parent)
    return [f"{rel}:{n}" for n in violations_in(path.read_text(encoding="utf-8"))]


def test_no_exception_is_logged_raw():
    offenders = [v for p in sorted(APP.rglob("*.py")) for v in _violations(p)]
    assert not offenders, (
        "log describe_exception(exc), not the exception itself:\n"
        + "\n".join(offenders)
    )


# "Guard the guard": each shape the detector is supposed to see, and the shapes it
# must leave alone, so removing a branch of it fails here rather than silently.
FLAGGED = {
    "bare": 'try:\n    f()\nexcept Exception as exc:\n    logger.error("x %s", exc)\n',
    "str": 'try:\n    f()\nexcept Exception as e:\n    logger.warning("x %s", str(e))\n',
    "fstring": 'try:\n    f()\nexcept Exception as e:\n    logger.warning(f"x {e!r}")\n',
    "args": 'try:\n    f()\nexcept Exception as e:\n    logger.warning("x %s", e.args)\n',
    "detail": 'try:\n    f()\nexcept Exception as e:\n    logger.warning("x %s", e.detail)\n',
    "getLogger receiver": (
        "try:\n    f()\nexcept Exception as exc:\n"
        '    logging.getLogger(__name__).error("x %s", exc)\n'
    ),
    "parameter": 'def h(exc):\n    logger.error("x %s", exc)\n',
    "derived name": (
        "def h():\n    last_error = None\n    try:\n        f()\n"
        "    except Exception as e:\n        last_error = e\n"
        '    logger.warning(f"gave up: {last_error}")\n'
    ),
    "derived via str": (
        "def h():\n    try:\n        f()\n    except Exception as exc:\n"
        "        msg = str(exc)\n"
        '    logger.error(f"x {msg}")\n'
    ),
}
CLEAN = {
    "describe_exception": (
        "try:\n    f()\nexcept Exception as exc:\n"
        '    logger.error("x %s", describe_exception(exc))\n'
    ),
    "type name": (
        "try:\n    f()\nexcept Exception as exc:\n"
        '    logger.error("x %s", type(exc).__name__)\n'
    ),
    "traceback channel": (
        'try:\n    f()\nexcept Exception as exc:\n    logger.error("x", exc_info=exc)\n'
    ),
    "comparison": 'def h(exc):\n    logger.error("x %s", exc is None)\n',
    "condition only": (
        'def h(exc):\n    logger.error("x %s", "none" if exc is None else "set")\n'
    ),
    "derived but sanitised": (
        "def h():\n    try:\n        f()\n    except Exception as exc:\n"
        "        msg = describe_exception(exc)\n"
        '    logger.error(f"x {msg}")\n'
    ),
    "unrelated name": 'try:\n    f()\nexcept Exception as exc:\n    logger.error("x %s", other)\n',
}


@pytest.mark.parametrize("source", FLAGGED.values(), ids=FLAGGED.keys())
def test_the_guard_flags(source):
    assert violations_in(source)


@pytest.mark.parametrize("source", CLEAN.values(), ids=CLEAN.keys())
def test_the_guard_leaves_alone(source):
    assert violations_in(source) == []
