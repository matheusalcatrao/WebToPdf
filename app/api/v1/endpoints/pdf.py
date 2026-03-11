import json

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
import os
import queue

from app.schemas.pdf import StartJobRequest
from app.services import pdf_processor

router = APIRouter()


@router.post("/start", response_model=None)
def start_job(body: StartJobRequest):
    url = body.url.strip()
    pdf_name = body.pdf_name.strip()

    if not url:
        return JSONResponse({"error": "URL is required"}, status_code=400)
    if not pdf_name.endswith(".pdf"):
        pdf_name += ".pdf"

    job_id = pdf_processor.start_pdf_job(url, pdf_name)
    if job_id is None:
        return JSONResponse(
            {"error": "A download is already running. Please wait."},
            status_code=429,
        )
    return {"job_id": job_id}


@router.get("/stream/{job_id}")
def stream(job_id: str):
    current_job = pdf_processor.get_current_job()
    if not current_job or current_job["id"] != job_id:
        raise HTTPException(status_code=404, detail="Job not found")

    q = current_job["queue"]

    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                continue

            if msg is None:  # sentinel
                job = pdf_processor.get_current_job() or {}
                status = job.get("status", "done")
                pdf = job.get("pdf", "chapter.pdf")
                yield f"data: {json.dumps({'type': 'done', 'status': status, 'pdf': pdf})}\n\n"
                break

            yield f"data: {json.dumps({'type': 'log', 'message': msg['text'], 'cr': msg['cr']})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/download")
def download(background_tasks: BackgroundTasks):
    current_job = pdf_processor.get_current_job()
    if not current_job or not current_job.get("pdf_path"):
        raise HTTPException(status_code=404, detail="No file available")

    filepath = current_job["pdf_path"]
    pdf_name = current_job["pdf"]

    if not os.path.exists(filepath):
        raise HTTPException(
            status_code=404,
            detail="File not found — it may have already been downloaded",
        )

    def cleanup():
        try:
            os.remove(filepath)
        except Exception:
            pass

    background_tasks.add_task(cleanup)
    return FileResponse(filepath, filename=pdf_name, media_type="application/pdf")
