import sys
import io
import json
import queue
import threading
import time
import os
import tempfile
from typing import Optional

from flask import Flask, render_template, request, jsonify, Response, stream_with_context, send_file

app = Flask(__name__)

# Only one job at a time
_job_lock = threading.Lock()
_current_job: Optional[dict] = None


class QueueWriter(io.TextIOBase):
    """Captures print() calls from a worker thread and feeds them into a Queue.

    Lines ending with \\r are flagged as in-place updates (progress bars) so
    the frontend can overwrite the last log line instead of appending a new one.
    """

    def __init__(self, q: queue.Queue):
        self._q = q
        self._buf = ""

    def write(self, s: str) -> int:
        for char in s:
            if char == "\n":
                if self._buf.strip():
                    self._q.put({"text": self._buf, "cr": False})
                self._buf = ""
            elif char == "\r":
                if self._buf.strip():
                    self._q.put({"text": self._buf, "cr": True})
                self._buf = ""
            else:
                self._buf += char
        return len(s)

    def flush(self):
        if self._buf.strip():
            self._q.put({"text": self._buf, "cr": False})
            self._buf = ""

    def readable(self):
        return False

    def writable(self):
        return True


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def start_job():
    global _current_job

    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    pdf_name = (data.get("pdf_name") or "chapter.pdf").strip()

    if not url:
        return jsonify({"error": "URL is required"}), 400
    if not pdf_name.endswith(".pdf"):
        pdf_name += ".pdf"

    if not _job_lock.acquire(blocking=False):
        return jsonify({"error": "A download is already running. Please wait."}), 429

    q: queue.Queue = queue.Queue()
    job_id = str(int(time.time() * 1000))
    tmp_dir = os.path.join(tempfile.gettempdir(), f"manga_{job_id}")
    pdf_path = os.path.join(tempfile.gettempdir(), pdf_name)
    _current_job = {"id": job_id, "queue": q, "pdf": pdf_name, "pdf_path": pdf_path, "status": "running"}

    def worker():
        global _current_job
        import main as manga_main

        old_stdout = sys.stdout
        sys.stdout = QueueWriter(q)
        try:
            manga_main.run(url, output_dir=tmp_dir, pdf_output=pdf_path)
            _current_job["status"] = "done"
        except Exception as exc:
            print(f"❌  Fatal error: {exc}")
            _current_job["status"] = "error"
        finally:
            # flush remaining buffer
            if hasattr(sys.stdout, "flush"):
                sys.stdout.flush()
            sys.stdout = old_stdout
            q.put(None)  # sentinel — signals stream end
            _job_lock.release()

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/stream/<job_id>")
def stream(job_id: str):
    global _current_job

    if not _current_job or _current_job["id"] != job_id:
        return jsonify({"error": "Job not found"}), 404

    q = _current_job["queue"]

    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
            except queue.Empty:
                # keep-alive heartbeat
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                continue

            if msg is None:  # sentinel
                status = (_current_job or {}).get("status", "done")
                pdf = (_current_job or {}).get("pdf", "chapter.pdf")
                yield f"data: {json.dumps({'type': 'done', 'status': status, 'pdf': pdf})}\n\n"
                break

            yield f"data: {json.dumps({'type': 'log', 'message': msg['text'], 'cr': msg['cr']})}\n\n"

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/download")
def download():
    if not _current_job or not _current_job.get("pdf_path"):
        return jsonify({"error": "No file available"}), 404
    filepath = _current_job["pdf_path"]
    pdf_name = _current_job["pdf"]
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found — it may have already been downloaded"}), 404

    from flask import after_this_request

    @after_this_request
    def _cleanup(response):
        try:
            os.remove(filepath)
        except Exception:
            pass
        return response

    return send_file(filepath, as_attachment=True, download_name=pdf_name)


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
