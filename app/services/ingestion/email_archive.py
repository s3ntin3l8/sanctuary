"""Members of a zip attached to an email, read without unpacking to disk."""

from __future__ import annotations

import io
import zipfile
from pathlib import PurePosixPath

from app import config
from app.services.ingestion.archive_guard import ArchiveRejected, check_zip

MAX_NESTING = 2  # a zip inside a zip inside the mail; deeper is refused
_JUNK = {".ds_store", "thumbs.db", "desktop.ini"}


def is_zip(filename: str | None, mime_type: str, data: bytes) -> bool:
    named = (filename or "").lower().endswith(".zip") or mime_type in (
        "application/zip",
        "application/x-zip-compressed",
    )
    return named and data[:4] in (b"PK\x03\x04", b"PK\x05\x06")


def archive_members(
    data: bytes, depth: int = 0, budget: list[int] | None = None
) -> list[dict]:
    """The files in a zip, each ``{name, size, data, outcome, reason, members}``.

    ``outcome`` is ``pending`` (to be ingested), ``archive`` (a nested zip, with
    its own ``members``) or ``unsupported``/``failed`` with a ``reason``. Raises
    ``ArchiveRejected`` when the archive itself is a bomb or unreadable.

    ``budget`` is a one-item list of the bytes that may still be unpacked for
    the whole mail (shared by every zip in it and by nested ones).
    """
    check_zip(data)
    limit = config.MAX_FILE_SIZE_MB * 1024 * 1024
    if budget is None:
        budget = [config.MAX_EMAIL_SIZE_MB * 1024 * 1024]
    members: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            name = PurePosixPath(info.filename.replace("\\", "/")).name
            if (
                info.is_dir()
                or not name
                or name.startswith(".")
                or name.lower() in _JUNK
                or "__MACOSX/" in info.filename
            ):
                continue
            member = {
                "name": name,
                "size": info.file_size,
                "data": b"",
                "outcome": "pending",
                "reason": None,
                "members": [],
            }
            members.append(member)
            if info.file_size > limit:
                member.update(outcome="unsupported", reason="File is too large")
                continue
            if info.file_size > budget[0]:
                member.update(
                    outcome="unsupported",
                    reason="Archives in this mail expand beyond the size limit",
                )
                continue
            try:
                content = archive.read(info)
            except RuntimeError, NotImplementedError:
                member.update(
                    outcome="unsupported",
                    reason="Password-protected or unsupported archive member",
                )
                continue
            except zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError:
                member.update(outcome="failed", reason="Unreadable archive member")
                continue
            member["data"] = content
            budget[0] -= len(content)
            if is_zip(name, "", content):
                if depth + 1 >= MAX_NESTING:
                    member.update(outcome="unsupported", reason="Archive nesting limit")
                    continue
                try:
                    member["members"] = archive_members(content, depth + 1, budget)
                except ArchiveRejected as exc:
                    member.update(outcome="unsupported", reason=str(exc))
                    continue
                if member["members"]:
                    member["outcome"] = "archive"
                else:
                    member.update(
                        outcome="unsupported", reason="Archive contains no files"
                    )
    return members


def is_tnef(filename: str | None, mime_type: str, data: bytes) -> bool:
    named = (
        (filename or "").lower() in ("winmail.dat", "win.dat")
        or (filename or "").lower().endswith(".tnef")
        or mime_type in ("application/ms-tnef", "application/vnd.ms-tnef")
    )
    return named and data[:4] == b"\x78\x9f\x3e\x22"


class TnefUnreadable(ValueError):
    """The bytes are not a readable TNEF stream (winmail.dat)."""


def tnef_members(data: bytes, budget: list[int] | None = None) -> list[dict]:
    """The files inside a winmail.dat, as ``archive_members`` returns them.

    A winmail.dat with no attachment is only Outlook's rich-text copy of the
    body: it yields no members. Raises ``TnefUnreadable``.
    """
    import tnefparse

    limit = config.MAX_FILE_SIZE_MB * 1024 * 1024
    if budget is None:
        budget = [config.MAX_EMAIL_SIZE_MB * 1024 * 1024]
    try:
        tnef = tnefparse.TNEF(data, do_checksum=False)
        attachments = list(tnef.attachments)
    except Exception as exc:  # tnefparse raises many types for bad input
        raise TnefUnreadable(str(exc)) from exc
    members: list[dict] = []
    for index, attachment in enumerate(attachments):
        name = f"attachment_{index}"
        try:
            content = bytes(attachment.data or b"")
        except Exception:  # noqa: BLE001 - one bad member fails only itself
            members.append(
                {
                    "name": name,
                    "size": 0,
                    "data": b"",
                    "outcome": "failed",
                    "reason": "Unreadable winmail.dat member",
                    "members": [],
                }
            )
            continue
        try:
            name = _tnef_name(attachment, index)
        except Exception:  # noqa: BLE001 - keep the content under a default name
            pass
        # A 0-byte member is kept: ingest labels it "empty", a visible review
        # item, instead of the winmail.dat looking like it had no attachment.
        member = {
            "name": name,
            "size": len(content),
            "data": b"",
            "outcome": "pending",
            "reason": None,
            "members": [],
        }
        members.append(member)
        if len(content) > limit:
            member.update(outcome="unsupported", reason="File is too large")
        elif len(content) > budget[0]:
            member.update(
                outcome="unsupported",
                reason="Archives in this mail expand beyond the size limit",
            )
        else:
            member["data"] = content
            budget[0] -= len(content)
            if is_zip(name, "", content):
                try:
                    member["members"] = archive_members(content, 1, budget)
                except ArchiveRejected as exc:
                    member.update(outcome="unsupported", reason=str(exc))
                else:
                    if member["members"]:
                        member["outcome"] = "archive"
                    else:
                        member.update(
                            outcome="unsupported", reason="Archive contains no files"
                        )
    return members


def _tnef_name(attachment, index: int) -> str:
    """The attachment's file name: the long name when the stream carries one.

    tnefparse decodes a bare title as UTF-8, which raises on the Windows-1252
    umlauts a title really holds; fall back to decoding the raw bytes ourselves.
    """
    try:
        name = attachment.long_filename()
    except UnicodeDecodeError:
        raw = getattr(attachment, "_name", b"")
        name = raw.decode("cp1252", errors="replace") if isinstance(raw, bytes) else raw
    if isinstance(name, bytes):
        name = name.decode("cp1252", errors="replace")
    name = PurePosixPath((name or "").strip("\x00").replace("\\", "/")).name
    return name or f"attachment_{index}"
