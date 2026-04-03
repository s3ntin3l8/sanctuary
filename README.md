# Sanctuary Legal

High-performance legal workspace application ("The Sanctuary") built for precision, speed, and focus.

## Overview

Sanctuary Legal is a modern workspace designed for legal professionals. It leverages the "Quiet Authority" design system to provide a distraction-free, high-performance environment for document management, case analysis, and legal research.

## Features

- **Quiet Authority Design System**: A premium, minimalist UI designed for professional legal counsel.
- **Dynamic Case Stream**: A high-performance dashboard for managing legal cases and streams.
- **Split-Pane Document Viewer**: Seamless, responsive navigation and interactive UI for complex legal documents.
- **Advanced File Ingestion**: Integrated with `docling` to convert uploaded files (PDFs, Docs, etc.) into structured markdown.
- **FastAPI & HTMX**: Minimal latency and modern interactivity without the overhead of heavy SPA frameworks.

## Technology Stack

- **Backend**: [FastAPI](https://fastapi.tiangolo.com/) (Python)
- **Frontend**: [HTMX](https://htmx.org/), [Alpine.js](https://alpinejs.dev/)
- **Styling**: [Tailwind CSS v4](https://tailwindcss.com/)
- **Database**: [SQLAlchemy](https://www.sqlalchemy.org/) (SQLite)
- **Document Processing**: [Docling](https://github.com/DS4SD/docling)

## Installation

1. **Clone the repository**:
   ```bash
   git clone git@github.com:s3ntin3l8/sanctuary.git
   cd sanctuary
   ```

2. **Set up the Python environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Install frontend dependencies**:
   ```bash
   npm install
   ```

## Running the Application

1. **Start the FastAPI dev server**:
   ```bash
   source venv/bin/activate
   python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
   ```

2. **Access the application**:
   Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

## License

Copyright © 2024 Sanctuary Legal. All rights reserved.
