from pydantic import BaseModel


class AboutView(BaseModel):
    version: str
    git_sha: str
    build_date: str
    db_revision: str | None
