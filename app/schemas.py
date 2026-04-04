from pydantic import BaseModel, ConfigDict
from typing import Optional, List
from datetime import datetime
from enum import Enum


class OriginatorType(str, Enum):
    COURT = "court"
    OPPOSING = "opposing"
    OWN = "own"
    UNKNOWN = "unknown"


class CaseStatus(str, Enum):
    INTAKE = "intake"
    DISCOVERY = "discovery"
    PRE_TRIAL = "pre_trial"
    TRIAL = "trial"
    POST_TRIAL = "post_trial"
    CLOSED = "closed"


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


# --- Deadline Schemas ---

class DeadlineBase(BaseModel):
    case_id: str
    title: str
    description: Optional[str] = None
    due_at: datetime
    completed: bool = False
    source_document_id: Optional[int] = None


class DeadlineCreate(DeadlineBase):
    pass


class DeadlineUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    due_at: Optional[datetime] = None
    completed: Optional[bool] = None
    source_document_id: Optional[int] = None


class Deadline(DeadlineBase):
    id: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


# --- Hearing Schemas ---

class HearingBase(BaseModel):
    case_id: str
    title: str
    description: Optional[str] = None
    scheduled_for: datetime
    location: Optional[str] = None
    source_document_id: Optional[int] = None


class HearingCreate(HearingBase):
    pass


class HearingUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    scheduled_for: Optional[datetime] = None
    location: Optional[str] = None
    source_document_id: Optional[int] = None


class Hearing(HearingBase):
    id: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


# --- Legal Cost Schemas ---

class CostCategory(str, Enum):
    GERICHTSKOSTEN       = "gerichtskosten"
    ANWALTSKOSTEN        = "anwaltskosten"
    ANWALTSKOSTEN_GEGNER = "anwaltskosten_gegner"
    SACHVERSTAENDIGER    = "sachverstaendiger"
    VORSCHUSS            = "vorschuss"
    VOLLSTRECKUNG        = "vollstreckung"
    AUSLAGEN             = "auslagen"
    SONSTIGES            = "sonstiges"


class CostStatus(str, Enum):
    OFFEN     = "offen"
    BEZAHLT   = "bezahlt"
    ERSTATTET = "erstattet"
    TEILWEISE = "teilweise"
    STRITTIG  = "strittig"


class LegalCostBase(BaseModel):
    case_id: str
    category: CostCategory
    status: CostStatus = CostStatus.OFFEN
    title: str
    rvg_position: Optional[str] = None
    amount_net: float
    vat_rate: float = 0.0
    amount_gross: float
    amount_paid: float = 0.0
    amount_reimbursed: float = 0.0
    streitwert: Optional[float] = None
    gebuehren_faktor: Optional[float] = None
    is_reimbursable: bool = True
    issued_at: Optional[datetime] = None
    due_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None
    notes: Optional[str] = None


class LegalCostCreate(LegalCostBase):
    pass


class LegalCostUpdate(BaseModel):
    status: Optional[CostStatus] = None
    amount_paid: Optional[float] = None
    amount_reimbursed: Optional[float] = None
    paid_at: Optional[datetime] = None
    notes: Optional[str] = None


class LegalCost(LegalCostBase):
    id: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
