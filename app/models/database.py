from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, Text, create_engine
from sqlalchemy.orm import declarative_base, relationship, validates
from datetime import datetime

Base = declarative_base()

class Document(Base):
    __tablename__ = 'documents'

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False, index=True)
    content = Column(Text, nullable=True)
    case_id = Column(String, nullable=True, index=True)
    file_path = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Self-referential relationship for 'Russian Doll' nesting
    parent_id = Column(Integer, ForeignKey('documents.id'), nullable=True)
    
    children = relationship('Document', back_populates='parent', cascade='all, delete-orphan')
    parent = relationship('Document', back_populates='children', remote_side=[id])

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
