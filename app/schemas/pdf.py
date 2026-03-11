from pydantic import BaseModel


class StartJobRequest(BaseModel):
    url: str
    pdf_name: str = "chapter.pdf"


class JobResponse(BaseModel):
    job_id: str


class ErrorResponse(BaseModel):
    error: str
