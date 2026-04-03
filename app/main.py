from contextlib import asynccontextmanager
from typing import Generator
from fastapi import FastAPI, Request, Depends, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.models.database import Base, engine, SessionLocal

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Setup DB on startup
    Base.metadata.create_all(bind=engine)
    yield

app = FastAPI(title="The Sanctuary", lifespan=lifespan)

# DB Dependency
def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="app/templates")

@app.get("/")
async def dashboard(request: Request):
    return templates.TemplateResponse("pages/dashboard.html", {"request": request})

from app.models.database import Document
from sqlalchemy.orm import Session

@app.get("/cases")
async def case_stream(request: Request, db: Session = Depends(get_db)):
    documents = db.query(Document).order_by(Document.created_at.desc()).all()
    return templates.TemplateResponse("pages/case_stream.html", {"request": request, "documents": documents})

@app.get("/document/{doc_id}")
async def get_document_details(request: Request, doc_id: str, db: Session = Depends(get_db)):
    # Retrieve the document securely
    doc = db.query(Document).filter(Document.id == doc_id).first()
    # Retrieve the partial for the HTMX request
    return templates.TemplateResponse("partials/document_details.html", {"request": request, "doc_id": doc_id, "doc": doc})

from app.services.ingestion import ingest_file

@app.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    case_id: str = Form(...),
    db = Depends(get_db)
):
    doc = await ingest_file(file, case_id, db)
    return {"message": "File ingested successfully", "doc_id": doc.id, "case_id": doc.case_id, "title": doc.title}
