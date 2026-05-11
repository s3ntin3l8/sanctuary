import io
import os
from datetime import UTC, datetime

from fastapi import UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, Case
from app.services.ingestion.service import ingest_file

# Setup test DB
DB_URL = "sqlite:///./test_verify.db"
engine = create_engine(DB_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    # Create a test case
    test_case = Case(id="TEST-001", title="Test Case")
    db.add(test_case)
    db.commit()
    return db


async def test_normalization():
    db = setup_db()

    # 1. Create a dummy PDF
    dummy_content = b"%PDF-1.4\n%TEST"
    filename = "My Random Scan @ 2023.pdf"

    # Mock UploadFile
    upload_file = UploadFile(
        filename=filename,
        file=io.BytesIO(dummy_content),
        headers={"content-type": "application/pdf"},
    )

    # 2. Ingest file
    print(f"Ingesting {filename}...")
    doc = await ingest_file(upload_file, db=db, skip_processing=True)

    print(
        f"Document created: ID={doc.id}, Title={doc.title}, Original Filename={doc.original_filename}"
    )
    assert doc.original_filename == filename

    # 3. Finalize (move out of triage)
    print("Finalizing document...")
    doc.case_id = "TEST-001"
    doc.title = "Verified Document Title"
    doc.issued_date = datetime(2023, 12, 25, tzinfo=UTC)
    doc.needs_review = False

    db.add(doc)
    db.commit()
    db.refresh(doc)

    print(f"New file path: {doc.file_path}")

    # Expected filename: 20231225_Verified_Document_Title.pdf
    expected_name = "20231225_Verified_Document_Title.pdf"
    assert expected_name in doc.file_path

    # Verify file exists
    from app.config import DATA_DIR

    full_path = DATA_DIR / doc.file_path
    assert full_path.exists()
    print(f"Verification successful! File moved to: {full_path}")

    # Cleanup
    db.close()
    if os.path.exists("./test_verify.db"):
        os.remove("./test_verify.db")
    if full_path.exists():
        os.remove(full_path)
    # also remove the triage file if it still exists (it shouldn't)
    triage_path = DATA_DIR / "_TRIAGE" / filename
    if triage_path.exists():
        os.remove(triage_path)


if __name__ == "__main__":
    import asyncio

    asyncio.run(test_normalization())
