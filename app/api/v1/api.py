from fastapi import APIRouter

from app.api.v1.endpoints import pdf

router = APIRouter()
router.include_router(pdf.router, tags=["pdf"])
