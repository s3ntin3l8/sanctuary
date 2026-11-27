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
"""

from contextvars import ContextVar
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


def for_document(doc_id: int) -> AiProvenance | None:
    """The provenance to store for ``doc_id``'s completed stage.

    ``None`` means "leave the stored values alone" (the document was not
    started in this scope). A started document whose stage made no AI call
    gets an all-empty provenance, so an earlier run's model is cleared.
    """
    scope = _scope.get()
    if scope is None or doc_id not in scope.doc_ids:
        return None
    return scope.provenance or AiProvenance(None, None, None)
