import enum
from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, Text, create_engine, Boolean, Enum as SAEnum, JSON
from sqlalchemy.orm import declarative_base, relationship, validates
from datetime import datetime


class CaseStatus(str, enum.Enum):
    INTAKE     = "intake"
    DISCOVERY  = "discovery"
    PRE_TRIAL  = "pre_trial"
    TRIAL      = "trial"
    POST_TRIAL = "post_trial"
    CLOSED     = "closed"


class OriginatorType(str, enum.Enum):
    """Maps to the border-l-4 originator stripes from GEMINI.md §4."""
    COURT = "court"           # Blue #0369A1 — Gavel icon
    OPPOSING = "opposing"     # Red  #B91C1C — Warning icon
    OWN = "own"               # Green #047857 — Shield icon
    UNKNOWN = "unknown"       # Neutral — for unclassified docs

Base = declarative_base()

class Document(Base):
    __tablename__ = 'documents'

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False, index=True)
    content = Column(Text, nullable=True)
    case_id = Column(String, nullable=True, index=True)
    file_path = Column(String, nullable=True)
    originator_type = Column(SAEnum(OriginatorType), default=OriginatorType.UNKNOWN, nullable=False)
    sender = Column(String, nullable=True)          # "Via: Email from [Sender] on [Date]"
    received_date = Column(DateTime, nullable=True) # When the physical document was received
    created_at = Column(DateTime, default=datetime.utcnow)
    needs_review = Column(Boolean, default=True, index=True)
    review_reasons = Column(JSON, default=list)  # e.g. ["missing_case_id", "missing_sender"]
    
    # Self-referential relationship for 'Russian Doll' nesting
    parent_id = Column(Integer, ForeignKey('documents.id'), nullable=True)
    
    children = relationship('Document', back_populates='parent', cascade='all, delete-orphan')
    parent = relationship('Document', back_populates='children', remote_side=[id])


class Case(Base):
    __tablename__ = 'cases'

    id         = Column(String, primary_key=True, index=True)   # Internal ID e.g. ADV-992-K
    title      = Column(String, nullable=False)
    court_id   = Column(String, nullable=True)                  # Official docket ID
    status     = Column(SAEnum(CaseStatus), default=CaseStatus.INTAKE, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    closed_at  = Column(DateTime, nullable=True)


class Expense(Base):
    __tablename__ = 'expenses'

    id = Column(Integer, primary_key=True, index=True)
    vendor = Column(String, nullable=False, index=True)
    amount = Column(Float, nullable=False)
    description = Column(String, nullable=True)
    date = Column(DateTime, default=datetime.utcnow)

    @validates('vendor', 'description')
    def validate_hm(self, key, value):
        """
        Ensure that if the vendor or description contains 'H&M',
        it is strictly saved in ALL CAPS.
        """
        if value and 'h&m' in value.lower():
            return value.upper()
        return value

# Database setup
import os
from sqlalchemy.orm import sessionmaker

# Create data directory if it doesn't exist
os.makedirs('./data', exist_ok=True)

SQLALCHEMY_DATABASE_URL = "sqlite:///./data/sanctuary.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
