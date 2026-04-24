from datetime import datetime

from sqlalchemy.orm import Session

from app.models.database import DocumentPin


class DocumentPinRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(
        self, document_id: int, passage_id: str, note: str | None = None
    ) -> DocumentPin:
        pin = DocumentPin(
            document_id=document_id,
            passage_id=passage_id,
            note=note,
            ingest_date=datetime.now(),
            updated_at=datetime.now(),
        )
        self.db.add(pin)
        self.db.flush()
        return pin

    def get(self, pin_id: int) -> DocumentPin | None:
        return self.db.query(DocumentPin).filter(DocumentPin.id == pin_id).first()

    def get_by_document(self, document_id: int) -> list[DocumentPin]:
        return (
            self.db.query(DocumentPin)
            .filter(DocumentPin.document_id == document_id)
            .order_by(DocumentPin.ingest_date.asc())
            .all()
        )

    def update_note(self, pin_id: int, note: str | None) -> DocumentPin | None:
        pin = self.get(pin_id)
        if pin is None:
            return None
        pin.note = note
        pin.updated_at = datetime.now()
        self.db.flush()
        return pin

    def delete(self, pin_id: int) -> bool:
        pin = self.get(pin_id)
        if pin is None:
            return False
        self.db.delete(pin)
        self.db.flush()
        return True
