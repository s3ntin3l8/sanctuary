from pydantic import BaseModel, ConfigDict
from typing import Optional, List
from datetime import datetime

# --- Document Schemas ---

class DocumentBase(BaseModel):
    title: str
    content: Optional[str] = None
    parent_id: Optional[int] = None

class DocumentCreate(DocumentBase):
    pass

class DocumentUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    parent_id: Optional[int] = None

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
