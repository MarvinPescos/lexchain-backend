"""
OCR service: the `/marker/upload` contract `MarkerOCRProvider` calls, with
signatures and seals removed before Marker reads a page.

Unlike the stock `marker_server`, which converts one request at a time, each
request runs in a pool of worker processes sharing one Surya server.
"""

import asyncio
import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, UploadFile

from . import marker_worker

log = logging.getLogger("ocr_service")

WORKERS = int(os.environ.get("OCR_WORKERS", "2"))
OUTPUT_FORMATS = ("json", "markdown", "html", "chunks")


@asynccontextmanager
async def lifespan(app: FastAPI):
    marker_worker.budget_inference(WORKERS)
    app.state.pool = ProcessPoolExecutor(
        max_workers=WORKERS,
        # CUDA and the Surya clients do not survive fork.
        mp_context=multiprocessing.get_context("spawn"),
        initializer=marker_worker.init,
    )
    yield
    app.state.pool.shutdown(cancel_futures=True)


app = FastAPI(title="LexChain OCR", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/marker/upload")
async def convert_upload(
    page_range: str | None = Form(default=None),
    mode: str | None = Form(default=None),
    force_ocr: bool = Form(default=False),
    paginate_output: bool = Form(default=False),
    output_format: str = Form(default="markdown"),
    file: UploadFile = File(..., media_type="application/pdf"),
):
    if output_format not in OUTPUT_FORMATS:
        return {"success": False, "error": f"Invalid output format: {output_format}"}

    pdf = await file.read()
    options = {
        "page_range": page_range,
        "mode": mode,
        "force_ocr": force_ocr,
        "paginate_output": paginate_output,
        "output_format": output_format,
    }
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(app.state.pool, marker_worker.convert, pdf, options)
    except Exception as exc:
        log.exception("marker conversion failed")
        return {"success": False, "error": str(exc)}
