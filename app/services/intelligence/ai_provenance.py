"""Which model and prompt version produced a pipeline stage's output.

A stage task opens a scope with ``pipeline_status.mark_started`` (``begin``),
every AI call inside it records what it used (``note``), and
``pipeline_status.mark_completed`` writes that onto the stage row
(``for_document``). The scope lives in a context variable, so it follows the
task through its own thread and nothing else.

A scope belongs to the documents started in it. A stage that starts several
documents before one AI call (BATCH_ANALYSIS) shares that call's provenance;
a document completed without having been started in the current scope (an
idempotent re-dispatch, a recovery sweep) leaves its stored provenance alone.
Completing a document takes it out of the scope, and every Celery task runs
in an empty one (``push`` on ``task_prerun``, ``pop`` on ``task_postrun`` in
``celery_app``), so a document is never credited with a call made for another
task. Push/pop rather than a plain reset keeps an enclosing task's scope intact
when a task is run eagerly from inside it.
"""

from contextvars import ContextVar, Token
from dataclasses import dataclass, field

from app.services.intelligence.prompts import PROMPT_VERSION


@dataclass(frozen=True)
class AiProvenance:
    model: str | None
    provider: str | None
    prompt_version: str | None


@dataclass
class _Scope:
    doc_ids: set[int] = field(default_factory=set)
    provenance: AiProvenance | None = None


_scope: ContextVar[_Scope | None] = ContextVar("ai_provenance_scope", default=None)


def push() -> Token:
    """Start an empty scope for a task; hand the token back to ``pop``."""
    return _scope.set(None)


def pop(token: Token) -> None:
    """Restore the scope that was current before the matching ``push``."""
    _scope.reset(token)


def begin(doc_id: int) -> None:
    """Open (or join) the scope for a stage that starts on ``doc_id``.

    Starts that follow each other before any AI call join one scope; once a
    call has been noted, the next start opens a fresh one.
    """
    scope = _scope.get()
    if scope is None or scope.provenance is not None:
        scope = _Scope()
        _scope.set(scope)
    scope.doc_ids.add(doc_id)


def note(
    model: str | None,
    provider: str | None,
    prompt_version: str | None = PROMPT_VERSION,
) -> None:
    """Record the model behind the current scope's output (the last call wins)."""
    scope = _scope.get()
    if scope is None:
        return
    scope.provenance = AiProvenance(
        model=model or None, provider=provider or None, prompt_version=prompt_version
    )


def forget(doc_id: int) -> None:
    """Take ``doc_id`` out of the scope without recording anything (its stage
    failed or was skipped), so a later completion cannot adopt this scope."""
    scope = _scope.get()
    if scope is not None:
        scope.doc_ids.discard(doc_id)


def for_document(doc_id: int) -> AiProvenance | None:
    """The provenance to store for ``doc_id``'s completed stage, consuming it.

    ``None`` means "leave the stored values alone" (the document was not
    started in this scope, or was already completed in it). A started document
    whose stage made no AI call gets an all-empty provenance, so an earlier
    run's model is cleared.
    """
    scope = _scope.get()
    if scope is None or doc_id not in scope.doc_ids:
        return None
    scope.doc_ids.discard(doc_id)
    return scope.provenance or AiProvenance(None, None, None)
