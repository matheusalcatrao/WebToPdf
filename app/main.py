import os

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

from app.api.v1.api import router as api_v1_router

app = FastAPI(title="WebToPdf")

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)

app.include_router(api_v1_router, prefix="/api/v1")


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    from app.core.config import settings

    uvicorn.run(app, host="0.0.0.0", port=settings.port)
