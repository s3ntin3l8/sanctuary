import os
import aiofiles
import asyncio
from fastapi import UploadFile
from sqlalchemy.orm import Session
from app.models.database import Document
from docling.document_converter import DocumentConverter

# Initialize the converter (this downloads models on first use if not present)
converter = DocumentConverter()

async def ingest_file(file: UploadFile, case_id: str, db: Session) -> Document:
    """
    Saves an uploaded file to a local directory grouped by case_id,
    converts it to Markdown using Docling, and stores the metadata
    and textual content into the database.
    """
    # 1. Ensure the destination directory exists
    case_dir = os.path.join("./data", case_id)
    os.makedirs(case_dir, exist_ok=True)
    
    # Secure the filename (basic safety)
    safe_filename = os.path.basename(file.filename)
    file_path = os.path.join(case_dir, safe_filename)
    
    # 2. Save the file to disk asynchronously
    async with aiofiles.open(file_path, 'wb') as out_file:
        # Read the file in chunks to be efficient
        while content := await file.read(1024 * 1024):  # 1MB chunks
            await out_file.write(content)
            
    # 3. Convert to markdown with docling.
    # Docling conversion is CPU intensive, so we run it in a thread pool to avoid blocking the async event loop.
    def convert_to_md(path: str) -> str:
        result = converter.convert(path)
        return result.document.export_to_markdown()
        
    markdown_content = await asyncio.to_thread(convert_to_md, file_path)
    
    # 4. Store the information in the database
    new_doc = Document(
        title=safe_filename,
        content=markdown_content,
        case_id=case_id,
        file_path=file_path
    )
    
    db.add(new_doc)
    db.commit()
    db.refresh(new_doc)
    
    return new_doc
