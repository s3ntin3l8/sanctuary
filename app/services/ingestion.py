from __future__ import annotations

import os
import re
import aiofiles
import asyncio
from typing import Optional
from fastapi import UploadFile
from sqlalchemy.orm import Session
from app.models.database import Document, OriginatorType
from docling.document_converter import DocumentConverter

# Initialize the converter (this downloads models on first use if not present)
converter = DocumentConverter()

# ---------------------------------------------------------------------------
# Heuristic metadata extraction from filename + content
# ---------------------------------------------------------------------------

# Common case-ID patterns: ADV-992-K, REF-441-22, 2023-CV-01234, etc.
CASE_ID_PATTERNS = [
    re.compile(r'\b(ADV-\d{3,4}-[A-Z]{1,3})\b', re.IGNORECASE),
    re.compile(r'\b(REF-\d{3,4}-\d{1,3})\b', re.IGNORECASE),
    re.compile(r'\b(\d{4}-CV-\d{4,6})\b', re.IGNORECASE),
    re.compile(r'\b(Case\s*#?\s*\d{3,6}-?[A-Z]{0,3})\b', re.IGNORECASE),
]

# Originator keywords: look for court / opposing / own-counsel signals
COURT_KEYWORDS = [
    'court order', 'court clerk', 'judge', 'subpoena', 'summons',
    'notice of motion', 'ruling', 'decree', 'judgment', 'tribunal',
    'magistrate', 'docket', 'hereby orders', 'it is ordered',
]
OPPOSING_KEYWORDS = [
    'opposing counsel', 'defendant', 'respondent', 'counter-claim',
    'demand letter', 'settlement offer', 'plaintiff', 'claimant',
    'blake & torres', 'counter-offer',
]
OWN_KEYWORDS = [
    'our client', 'memo to file', 'internal memo', 'work product',
    'privileged', 'expert witness', 'draft', 'strategy',
]

# Date patterns in content
DATE_PATTERNS = [
    re.compile(r'(?:received|dated|filed|sent)\s+(?:on\s+)?(\w+ \d{1,2},? \d{4})', re.IGNORECASE),
    re.compile(r'(\d{4}-\d{2}-\d{2})'),
    re.compile(r'(\d{1,2}/\d{1,2}/\d{4})'),
]

# Sender patterns
SENDER_PATTERNS = [
    re.compile(r'(?:from|sender|by|signed|submitted by)[:\s]+([A-Z][a-z]+ [A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', re.IGNORECASE),
    re.compile(r'(?:from|sender)[:\s]+([A-Z][a-z]+ (?:&|and) [A-Z][a-z]+ (?:LLP|LLC|PC|PLLC))', re.IGNORECASE),
]


def extract_case_id(filename: str, content: str) -> str | None:
    """Try to extract a case ID from filename first, then from content."""
    for pattern in CASE_ID_PATTERNS:
        match = pattern.search(filename)
        if match:
            return match.group(1).upper()
    # Search only first 2000 chars of content for performance
    snippet = (content or '')[:2000]
    for pattern in CASE_ID_PATTERNS:
        match = pattern.search(snippet)
        if match:
            return match.group(1).upper()
    return None


def extract_originator(filename: str, content: str) -> OriginatorType:
    """Classify originator based on keyword matching."""
    combined = (filename + ' ' + (content or '')[:3000]).lower()
    court_score = sum(1 for kw in COURT_KEYWORDS if kw in combined)
    opposing_score = sum(1 for kw in OPPOSING_KEYWORDS if kw in combined)
    own_score = sum(1 for kw in OWN_KEYWORDS if kw in combined)

    best = max(court_score, opposing_score, own_score)
    if best == 0:
        return OriginatorType.UNKNOWN
    if court_score == best:
        return OriginatorType.COURT
    if opposing_score == best:
        return OriginatorType.OPPOSING
    return OriginatorType.OWN


def extract_received_date(content: str):
    """Try to extract a date from document content."""
    from datetime import datetime as dt
    snippet = (content or '')[:3000]
    for pattern in DATE_PATTERNS:
        match = pattern.search(snippet)
        if match:
            date_str = match.group(1)
            for fmt in ('%B %d, %Y', '%B %d %Y', '%Y-%m-%d', '%m/%d/%Y'):
                try:
                    return dt.strptime(date_str.replace(',', ''), fmt.replace(',', ''))
                except ValueError:
                    continue
    return None


def extract_sender(content: str) -> str | None:
    """Try to extract a sender name from content."""
    snippet = (content or '')[:3000]
    for pattern in SENDER_PATTERNS:
        match = pattern.search(snippet)
        if match:
            return match.group(1).strip()
    return None


def extract_clean_title(filename: str) -> str:
    """Convert filename into a human-readable title."""
    name = os.path.splitext(filename)[0]
    # Replace underscores, hyphens with spaces
    name = re.sub(r'[_\-]+', ' ', name)
    # Remove case ID prefixes if present
    for pattern in CASE_ID_PATTERNS:
        name = pattern.sub('', name)
    name = name.strip()
    if name:
        return name.title()
    return filename


def compute_review_reasons(doc: Document) -> list[str]:
    """
    Compute a list of review reason codes based on which metadata fields
    are still missing or were not extracted. This is the single source of
    truth for whether a document needs human review.
    """
    reasons = []
    if not doc.case_id:
        reasons.append("missing_case_id")
    if doc.originator_type == OriginatorType.UNKNOWN:
        reasons.append("missing_originator")
    if not doc.sender:
        reasons.append("missing_sender")
    if not doc.received_date:
        reasons.append("missing_received_date")
    # Title is the raw filename — still counts as "needs review" for renaming
    if doc.title and '.' in doc.title and doc.title == os.path.basename(doc.file_path or ''):
        reasons.append("missing_title")
    if not doc.content or len(doc.content.strip()) < 20:
        reasons.append("missing_content")
    return reasons


# ---------------------------------------------------------------------------
# Main ingestion pipeline
# ---------------------------------------------------------------------------

async def ingest_file(file: UploadFile, case_id: str, db: Session, parent_id: int = None) -> Document:
    """
    Saves an uploaded file to a local directory grouped by case_id,
    converts it to Markdown using Docling, runs heuristic metadata
    extraction, and stores the result in the database.
    
    Documents with incomplete metadata are flagged for triage review.
    """
    # 1. Ensure the destination directory exists
    case_dir = os.path.join("./data", case_id or "_triage")
    os.makedirs(case_dir, exist_ok=True)
    
    # Secure the filename (basic safety)
    safe_filename = os.path.basename(file.filename)
    file_path = os.path.join(case_dir, safe_filename)
    
    # 2. Save the file to disk asynchronously
    async with aiofiles.open(file_path, 'wb') as out_file:
        while content := await file.read(1024 * 1024):  # 1MB chunks
            await out_file.write(content)
            
    # 3. Convert to markdown with docling
    def convert_to_md(path: str) -> str:
        result = converter.convert(path)
        return result.document.export_to_markdown()
        
    markdown_content = await asyncio.to_thread(convert_to_md, file_path)
    
    # 4. Heuristic metadata extraction
    extracted_case_id = extract_case_id(safe_filename, markdown_content)
    extracted_originator = extract_originator(safe_filename, markdown_content)
    extracted_date = extract_received_date(markdown_content)
    extracted_sender = extract_sender(markdown_content)
    extracted_title = extract_clean_title(safe_filename)

    # Prefer the explicitly provided case_id, fall back to extraction
    final_case_id = case_id if case_id else extracted_case_id

    # 5. Build the document
    new_doc = Document(
        title=extracted_title if extracted_title != safe_filename else safe_filename,
        content=markdown_content,
        case_id=final_case_id,
        file_path=file_path,
        parent_id=parent_id,
        originator_type=extracted_originator,
        sender=extracted_sender,
        received_date=extracted_date,
    )
    
    # 6. Compute review reasons BEFORE persisting
    reasons = compute_review_reasons(new_doc)
    new_doc.review_reasons = reasons
    new_doc.needs_review = len(reasons) > 0

    db.add(new_doc)
    db.commit()
    db.refresh(new_doc)
    
    return new_doc
