from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_index_returns_html():
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_start_job_missing_url_returns_400():
    response = client.post("/api/v1/start", json={"url": ""})
    assert response.status_code == 400
    assert "error" in response.json()


def test_start_job_whitespace_url_returns_400():
    response = client.post("/api/v1/start", json={"url": "   "})
    assert response.status_code == 400
    assert "error" in response.json()


def test_stream_unknown_job_returns_404():
    response = client.get("/api/v1/stream/nonexistent-job-id")
    assert response.status_code == 404


def test_download_no_job_returns_404():
    response = client.get("/api/v1/download")
    assert response.status_code == 404


def test_openapi_schema_available():
    response = client.get("/openapi.json")
    assert response.status_code == 200
    data = response.json()
    assert data["info"]["title"] == "WebToPdf"


def test_start_job_busy_returns_429():
    # Simulate a job already running by making start_pdf_job return None
    with patch(
        "app.api.v1.endpoints.pdf.pdf_processor.start_pdf_job", return_value=None
    ):
        response = client.post("/api/v1/start", json={"url": "http://example.com"})
    assert response.status_code == 429
    assert "error" in response.json()
