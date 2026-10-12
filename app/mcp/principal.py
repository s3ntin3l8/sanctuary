"""Who is calling, and which cases they can reach.

The single place MCP tools get access decisions from. A token acts as its owner
and can never exceed them: every decision is the owner's visibility/edit rights
(`access_service`) intersected with the token's own scope. A case that is
missing and a case that is out of scope produce the same error, like the REST
API's 404s, so an agent cannot probe for what exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy.orm import Session

from app.core.timezone import now_utc
from app.models.database import ActionItem, ApiToken, Case, Claim, Document, User
from app.models.enums import ApiTokenAccess
from app.services import access_service

NOT_FOUND = "Not found, or outside this token's scope."
READ_ONLY = "This token is read-only."
_TRIAGE = "_TRIAGE"


@dataclass(frozen=True)
class McpPrincipal:
    user: User
    token_id: int
    token_name: str
    access: ApiTokenAccess
    all_cases: bool
    scoped_case_ids: frozenset[str]
    # One principal serves one tool call (one DB session), so the owner's case
    # sets are computed once per call instead of once per checked row.
    _memo: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def load(cls, db: Session, *, token_id: int | None) -> McpPrincipal:
        """The principal for ``token_id`` (set by the auth gate); fails closed."""
        token = db.get(ApiToken, token_id) if token_id is not None else None
        if (
            token is None
            or token.revoked_at is not None
            or (token.expires_at is not None and token.expires_at <= now_utc())
            or not token.user.is_active
        ):
            raise ToolError("Not authenticated.")
        return cls(
            user=token.user,
            token_id=token.id,
            token_name=token.name,
            access=token.access,
            all_cases=token.all_cases,
            scoped_case_ids=frozenset(c.case_id for c in token.cases),
        )

    @property
    def audit_label(self) -> str:
        return f"mcp:{self.token_name}"

    @property
    def can_write(self) -> bool:
        return self.access == ApiTokenAccess.READ_WRITE

    def _narrow(self, allowed: set[str] | None) -> set[str] | None:
        """Intersect an owner-level case set (None = unrestricted) with the scope."""
        if self.all_cases:
            return allowed
        scope = set(self.scoped_case_ids)
        return scope if allowed is None else scope & allowed

    def readable_case_ids(self, db: Session) -> set[str] | None:
        """Case ids this token may read; ``None`` means every case."""
        if "readable" not in self._memo:
            self._memo["readable"] = self._narrow(
                access_service.visible_case_ids(db, self.user)
            )
        found = self._memo["readable"]
        return None if found is None else set(found)

    def writable_case_ids(self, db: Session) -> set[str] | None:
        if not self.can_write:
            return set()
        if "writable" not in self._memo:
            self._memo["writable"] = self._narrow(
                access_service.editable_case_ids(db, self.user)
            )
        found = self._memo["writable"]
        return None if found is None else set(found)

    def can_read_object(
        self, db: Session, *, owner_id: int | None, case_id: str | None
    ) -> bool:
        """Row-level read test. Untriaged rows need an all-cases token and ownership."""
        if not case_id or case_id == _TRIAGE:
            return self.all_cases and access_service.can_see_object(
                self.user, None, owner_id=owner_id, case_id=case_id
            )
        readable = self.readable_case_ids(db)
        return readable is None or case_id in readable

    def require_case(self, db: Session, case_id: str, *, write: bool = False) -> Case:
        case = db.get(Case, case_id) if case_id and case_id != _TRIAGE else None
        if case is None:
            raise ToolError(NOT_FOUND)
        readable = self.readable_case_ids(db)
        if readable is not None and case.id not in readable:
            raise ToolError(NOT_FOUND)
        if write:
            if not self.can_write:
                raise ToolError(READ_ONLY)
            writable = self.writable_case_ids(db)
            if writable is not None and case.id not in writable:
                raise ToolError("You can view this case but not edit it.")
        return case

    def require_document(
        self, db: Session, document_id: int, *, write: bool = False
    ) -> Document:
        doc = db.get(Document, document_id)
        if doc is None or not self.can_read_object(
            db, owner_id=doc.owner_id, case_id=doc.case_id
        ):
            raise ToolError(NOT_FOUND)
        if write:
            if not self.can_write:
                raise ToolError(READ_ONLY)
            if not doc.case_id or doc.case_id == _TRIAGE:
                return doc  # own triage item; readable implies owner/admin
            writable = self.writable_case_ids(db)
            if writable is not None and doc.case_id not in writable:
                raise ToolError("You can view this document but not edit it.")
        return doc

    def require_claim(
        self, db: Session, claim_id: int, *, write: bool = False
    ) -> Claim:
        """A claim the token may read (or edit).

        Claims are global: one claim can be evidenced from several cases. Reading
        needs one linked case that is both visible to the owner and in the
        token's scope; editing also needs owner edit access (as the REST API
        decides it) and every owner-visible linked case inside the token's
        writable set, since an edit changes the claim in all of them.
        """
        claim = db.get(Claim, claim_id)
        if claim is None or claim.dismissed_at is not None:
            raise ToolError(NOT_FOUND)
        if not access_service.claim_access_allowed(db, self.user, claim_id, edit=False):
            raise ToolError(NOT_FOUND)
        linked = access_service.claim_linked_case_ids(db, claim_id)
        visible: set[str] = set()
        if linked:
            visible = {
                c.id
                for c in db.query(Case).filter(Case.id.in_(linked))
                if access_service.can_view_case(db, self.user, c)
            }
            readable = self.readable_case_ids(db)
            if not (visible if readable is None else visible & readable):
                raise ToolError(NOT_FOUND)
        elif not self.all_cases:
            # No linked case: the claim hangs only off untriaged documents. Like
            # untriaged documents themselves, that is outside any per-case token's
            # scope (the owner can still reach it with an all-cases token).
            raise ToolError(NOT_FOUND)
        if write:
            if not self.can_write:
                raise ToolError(READ_ONLY)
            if not access_service.claim_access_allowed(
                db, self.user, claim_id, edit=True
            ):
                raise ToolError("You can view this claim but not edit it.")
            writable = self.writable_case_ids(db)
            # (a claim with no linked case leaves `visible` empty: only all-cases
            # tokens get here, and the ownership check above is the gate)
            if writable is not None and not visible <= writable:
                raise ToolError(
                    "This claim also belongs to cases outside this token's write scope."
                )
        return claim

    def require_action_item(
        self, db: Session, action_item_id: int, *, write: bool = False
    ) -> ActionItem:
        item = db.get(ActionItem, action_item_id)
        if item is None:
            raise ToolError(NOT_FOUND)
        self.require_case(db, item.case_id, write=write)
        return item
