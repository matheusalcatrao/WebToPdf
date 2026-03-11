import sys
import io
import os
import queue
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests
from PIL import Image
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from app.core.config import settings

# ─── Job state ────────────────────────────────────────────────────────────────

_job_lock = threading.Lock()
_current_job: Optional[dict] = None


# ─── Queue writer ─────────────────────────────────────────────────────────────

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


# ─── Scraping helpers ─────────────────────────────────────────────────────────

def _fmt_bytes(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _collect_image_urls(driver):
    """Return all manga-page image URLs found in the DOM (handles lazy-load)."""
    img_elements = driver.find_elements(By.TAG_NAME, "img")
    urls = []
    seen = set()
    for el in img_elements:
        for attr in ("src", "data-src", "data-lazy-src", "data-original",
                     "data-url", "data-image", "srcset"):
            val = el.get_attribute(attr) or ""
            if attr == "srcset" and val:
                val = val.strip().split()[-2] if len(val.strip().split()) >= 2 else val.strip().split()[0]
            val = val.strip()
            if val.startswith("http") and val not in seen:
                urls.append(val)
                seen.add(val)
                break
    return urls


def _scroll_and_collect(driver):
    """Slowly scroll the whole page so lazy-loaded images get their src set."""
    last_height = driver.execute_script("return document.body.scrollHeight")
    collected: set[str] = set()
    prev_count = 0

    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(1)

    step = 4000
    current_pos = 0

    print(f"  📐  Page height: {last_height}px — scrolling in {step}px steps")

    while True:
        current_pos += step
        driver.execute_script(f"window.scrollTo(0, {current_pos});")
        time.sleep(settings.scroll_pause)

        for url in _collect_image_urls(driver):
            collected.add(url)

        new_height = driver.execute_script("return document.body.scrollHeight")
        pct = min(100, int(current_pos / new_height * 100))

        if len(collected) != prev_count:
            print(f"  🔍  [{pct:3d}%] pos={current_pos}px  images found so far: {len(collected)}")
            prev_count = len(collected)
        else:
            print(f"  ⏩  [{pct:3d}%] pos={current_pos}px", end="\r")

        if current_pos >= new_height:
            print(f"\n  🏁  Reached bottom ({new_height}px) — doing final pass…")
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1)
            for url in _collect_image_urls(driver):
                collected.add(url)
            break
        last_height = new_height

    ordered = []
    seen = set()
    for url in _collect_image_urls(driver):
        if url not in seen:
            ordered.append(url)
            seen.add(url)
    extra = 0
    for url in collected:
        if url not in seen:
            ordered.append(url)
            extra += 1
    if extra:
        print(f"  ➕  {extra} extra URL(s) found mid-scroll (not in final DOM)")
    return ordered


def _download_image(url, referer, session):
    headers = {
        "Referer": referer,
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    resp = session.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.content, len(resp.content)


def _images_to_pdf(image_paths, output_path):
    print(f"\n📄  Building PDF from {len(image_paths)} image(s)…")
    pages = []
    for i, path in enumerate(image_paths, 1):
        img = Image.open(path).convert("RGB")
        pages.append(img)
        print(f"  📎  [{i}/{len(image_paths)}] {os.path.basename(path)}  {img.width}×{img.height}px")
    if not pages:
        print("❌  No images to convert.")
        return
    print("  💾  Writing PDF…")
    pages[0].save(
        output_path,
        save_all=True,
        append_images=pages[1:],
        resolution=150,
    )
    size = os.path.getsize(output_path)
    print(f"\n✅  PDF saved → {output_path}  ({len(pages)} pages, {_fmt_bytes(size)})")


# ─── Main pipeline ────────────────────────────────────────────────────────────

def run(url, output_dir, pdf_output):
    """Run the full manga-to-PDF pipeline. All output goes to sys.stdout."""
    start_time = time.time()
    print("╔══════════════════════════════════════════╗")
    print("║          Manga → PDF Downloader          ║")
    print("╚══════════════════════════════════════════╝")
    print(f"🎯  URL        : {url}")
    print(f"📁  Output dir : {output_dir}")
    print(f"📄  PDF output : {pdf_output}")
    print()

    if os.path.exists(output_dir):
        print(f"🗑   Cleaning old output dir '{output_dir}'…")
        shutil.rmtree(output_dir)
    os.makedirs(output_dir)
    print(f"📂  Created output dir '{output_dir}'")
    print()

    print("🚀  Launching headless Chrome…")
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1280,900")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )
    print("✅  Chrome ready")
    print(f"🌐  Opening {url}")
    driver.get(url)
    print("⏳  Waiting for page content to appear…")
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "img"))
        )
        print(f"✅  Page loaded: '{driver.title}'")
    except Exception:
        print("⚠️  Timeout waiting for images — continuing anyway")
    print()

    print("📜  Scrolling page to trigger lazy-load…")
    img_urls = _scroll_and_collect(driver)
    driver.quit()
    print("✅  Browser closed")
    print()

    print(f"🖼   Found {len(img_urls)} image URL(s) total")
    for j, u in enumerate(img_urls, 1):
        print(f"  {j:3d}. {u[:100]}")
    print()

    if not img_urls:
        print("❌  No images found. The site structure may have changed.")
        return

    print(f"⬇️   Downloading {len(img_urls)} image(s) with {settings.download_workers} parallel workers…")
    session = requests.Session()
    results = {}
    total_bytes = 0
    skipped = 0

    def _fetch(idx_url):
        idx, img_url = idx_url
        data, size = _download_image(img_url, referer=url, session=session)
        img = Image.open(io.BytesIO(data))
        img.verify()
        ext = img.format.lower() if img.format else "jpg"
        path = os.path.join(output_dir, f"{idx:04d}.{ext}")
        with open(path, "wb") as f:
            f.write(data)
        return idx, path, size

    with ThreadPoolExecutor(max_workers=settings.download_workers) as pool:
        futures = {pool.submit(_fetch, (i, u)): i for i, u in enumerate(img_urls)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                idx, path, size = fut.result()
                results[idx] = path
                total_bytes += size
                done = len(results) + skipped
                print(f"  ↓  [{done:3d}/{len(img_urls)}] {os.path.basename(path)}  ({_fmt_bytes(size)})")
            except Exception as e:
                skipped += 1
                done = len(results) + skipped
                print(f"  ⚠️   Skipped [{done}/{len(img_urls)}] #{i}  ({e})")

    saved_files = [results[k] for k in sorted(results)]
    elapsed = time.time() - start_time
    print(f"\n✅  Downloaded {len(saved_files)}/{len(img_urls)} page(s)  — {_fmt_bytes(total_bytes)} total")
    print(f"⏱   Time so far: {elapsed:.1f}s")

    if saved_files:
        _images_to_pdf(saved_files, pdf_output)
        elapsed = time.time() - start_time
        print(f"⏱   Total time : {elapsed:.1f}s")

    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
        print(f"🗑   Removed temp dir '{output_dir}'")


# ─── Job management ───────────────────────────────────────────────────────────

def start_pdf_job(url: str, pdf_name: str) -> Optional[str]:
    """Start a background PDF generation job. Returns job_id, or None if busy."""
    global _current_job

    if not _job_lock.acquire(blocking=False):
        return None

    q: queue.Queue = queue.Queue()
    job_id = str(int(time.time() * 1000))
    tmp_dir = os.path.join(tempfile.gettempdir(), f"manga_{job_id}")
    pdf_path = os.path.join(tempfile.gettempdir(), pdf_name)
    _current_job = {
        "id": job_id,
        "queue": q,
        "pdf": pdf_name,
        "pdf_path": pdf_path,
        "status": "running",
    }

    def worker():
        global _current_job
        old_stdout = sys.stdout
        sys.stdout = QueueWriter(q)
        try:
            run(url, output_dir=tmp_dir, pdf_output=pdf_path)
            _current_job["status"] = "done"
        except Exception as exc:
            print(f"❌  Fatal error: {exc}")
            _current_job["status"] = "error"
        finally:
            if hasattr(sys.stdout, "flush"):
                sys.stdout.flush()
            sys.stdout = old_stdout
            q.put(None)  # sentinel — signals stream end
            _job_lock.release()

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def get_current_job() -> Optional[dict]:
    """Return the current job dict, or None if no job has been started."""
    return _current_job
