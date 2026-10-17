"""Readable text of an iCalendar (RFC 5545) invitation, for ingest and review."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.config import TIMEZONE

_LABELS = (
    ("DTSTART", "Beginn"),
    ("DTEND", "Ende"),
    ("LOCATION", "Ort"),
    ("ORGANIZER", "Organisator"),
    ("ATTENDEE", "Teilnehmer"),
)


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        elif line:
            lines.append(line)
    return lines


def _unescape(value: str) -> str:
    return re.sub(
        r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value
    ).strip()


def _property(line: str) -> tuple[str, dict[str, str], str] | None:
    head, sep, value = line.partition(":")
    if not sep:
        return None
    name, *raw_params = head.split(";")
    params = {}
    for param in raw_params:
        key, _, val = param.partition("=")
        params[key.upper()] = val.strip('"')
    return name.upper(), params, value


def _when(value: str, params: dict[str, str]) -> str:
    """A DTSTART/DTEND value in the display timezone (a date-only value stays a date)."""
    value = value.strip()
    try:
        if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
            return date(int(value[:4]), int(value[4:6]), int(value[6:8])).strftime(
                "%d.%m.%Y"
            )
        naive = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
        if value.endswith("Z"):
            moment = naive.replace(tzinfo=UTC)
        else:
            moment = naive.replace(tzinfo=ZoneInfo(params.get("TZID") or TIMEZONE))
        return moment.astimezone(ZoneInfo(TIMEZONE)).strftime("%d.%m.%Y %H:%M")
    except (ValueError, KeyError, OSError):
        return value


def _person(value: str, params: dict[str, str]) -> str:
    address = re.sub(r"(?i)^mailto:", "", value.strip())
    name = params.get("CN")
    return f"{name} <{address}>" if name else address


def ics_to_text(text: str) -> str:
    """Markdown for each VEVENT; "" when the file holds none."""
    events: list[dict] = []
    method = ""
    current: dict | None = None
    for line in _unfold(text):
        prop = _property(line)
        if prop is None:
            continue
        name, params, value = prop
        if name == "BEGIN" and value.upper() == "VEVENT":
            current = {}
        elif name == "END" and value.upper() == "VEVENT" and current is not None:
            events.append(current)
            current = None
        elif name == "METHOD":
            method = value.strip().upper()
        elif current is not None:
            current.setdefault(name, []).append((params, value))
    blocks = []
    for event in events:
        title = _unescape(event.get("SUMMARY", [({}, "")])[0][1]) or "(ohne Titel)"
        lines = [f"# Termin: {title}"]
        if method == "CANCEL":
            lines.append("- Status: abgesagt")
        for key, label in _LABELS:
            for params, value in event.get(key, []):
                if key in ("DTSTART", "DTEND"):
                    rendered = _when(value, params)
                elif key in ("ORGANIZER", "ATTENDEE"):
                    rendered = _person(value, params)
                else:
                    rendered = _unescape(value)
                if rendered:
                    lines.append(f"- {label}: {rendered}")
        description = _unescape(event.get("DESCRIPTION", [({}, "")])[0][1])
        if description:
            lines += ["", description]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
