from pydantic import BaseModel, ConfigDict
from typing import Optional, List
from datetime import datetime
from enum import Enum


class OriginatorType(str, Enum):
    COURT = "court"
    OPPOSING = "opposing"
    OWN = "own"
    UNKNOWN = "unknown"


# --- Document Schemas ---

class DocumentBase(BaseModel):
    title: str
    content: Optional[str] = None
    case_id: Optional[str] = None
    file_path: Optional[str] = None
    parent_id: Optional[int] = None
    originator_type: OriginatorType = OriginatorType.UNKNOWN
    sender: Optional[str] = None
    received_date: Optional[datetime] = None
    needs_review: bool = True

class DocumentCreate(DocumentBase):
    pass

class DocumentUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    case_id: Optional[str] = None
    parent_id: Optional[int] = None
    originator_type: Optional[OriginatorType] = None
    sender: Optional[str] = None
    received_date: Optional[datetime] = None
    needs_review: Optional[bool] = None

class Document(DocumentBase):
    id: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class DocumentWithChildren(Document):
    children: List['DocumentWithChildren'] = []

# Resolve forward references
DocumentWithChildren.model_rebuild()

# --- Expense Schemas ---

class ExpenseBase(BaseModel):
    vendor: str
    amount: float
    description: Optional[str] = None

class ExpenseCreate(ExpenseBase):
    pass

class ExpenseUpdate(BaseModel):
    vendor: Optional[str] = None
    amount: Optional[float] = None
    description: Optional[str] = None

class Expense(ExpenseBase):
    id: int
    date: datetime
    model_config = ConfigDict(from_attributes=True)
