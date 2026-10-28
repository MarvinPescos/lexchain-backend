# OCR service

Marker 2.0 behind the `POST /marker/upload` contract the backend's
`MarkerOCRProvider` calls, with signatures, seals and stamps removed from each
page before Marker reads it. Surya 2 returns a bare `<img/>` for any region
holding handwriting or a stamp and drops the printed text inside it (signer
names, the notary block, the Doc/Page/Book/Series column); with the ink gone
it reads that text.

It needs Marker's internals (two passes over its document model and a custom
processor), so it runs here, on the GPU host next to Surya's vLLM server, not
in the backend.

## What happens to a document

1. Every page is rendered at 200 DPI and **coloured ink** (seals, stamps,
   blue/red signatures) is whitened: saturation ≥ 35, chroma ≥ 25 and not
   paper. The mask grows 3 px but never into neutral print, and a seal's pale
   ghost inside each region is cleared.
2. **Marker, first pass**, on the cleaned page. Every non-Picture block with no
   visible text is where a black signature is.
3. In each such block, the handwriting **above the signature line** is erased.
   A block with no signature line is left alone.
4. **Marker, second pass**, only if step 3 erased something.
5. Every removed region comes back in `metadata.removed_regions` as
   `{page, bbox_pt, kind, image}` (`kind` is `coloured ink` or `signature`,
   `page` is 0-based, `image` names a PNG crop of the original pixels in
   `images`). Markdown and HTML output also get a "Signatures and seals"
   section.

Marker's `MarginaliaProcessor` is off: on legal-size pages its top-8% header
zone swallows the document title. An extra processor re-asks Surya, in block
mode as Text, about Picture/Figure and image-only blocks, keeping the answer
only when it has text.

Code: `cleaning.py` (pixels), `pipeline.py` (the passes, Marker injected),
`processors.py` and `marker_worker.py` (Marker), `server.py` (HTTP).

## Running

Requests run in a pool of `OCR_WORKERS` processes sharing one Surya server;
its capacity is split across them through `SURYA_INFERENCE_PARALLEL` unless
that is set.

```bash
docker build -t lexchain-ocr ocr-service
docker run --network host \
  -e SURYA_INFERENCE_URL=http://127.0.0.1:8765/v1 -e VLLM_GPU_TYPE=t4 \
  -e OCR_WORKERS=2 lexchain-ocr
```

Point the backend at it with `OCR_ENGINE=marker` and
`MARKER_API_URL=http://<host>:8001`.

### Surya on a 12 GB card under WSL (RTX 3060, Docker Desktop)

Image `vllm/vllm-openai:v0.20.1`, model `datalab-to/surya-ocr-2`, with:

- `--enforce-eager`: torch.compile / CUDA-graph capture otherwise exhausts WSL
  RAM and the engine dies silently.
- `--limit-mm-per-prompt.image 1 --limit-mm-per-prompt.video 0
  --mm-processor-kwargs.min_pixels 3136
  --mm-processor-kwargs.max_pixels 6291456`: vLLM otherwise profiles a
  full-size dummy video at startup and runs out of RAM.
- `--max-num-seqs 16 --max-num-batched-tokens 4096
  --gpu-memory-utilization 0.80`.
- Bind-mount the Hugging Face cache to a plain folder; `%USERPROFILE%\.cache`
  could not be mounted.

## Tests

```bash
pip install -r requirements.txt pytest httpx
pytest
```

`tests/test_marker_processing.py` needs Marker installed (CPU torch is
enough) and is skipped without it; the rest need only numpy, Pillow,
pypdfium2 and FastAPI.

## Known limits

- A black signature with no line under it is left in place.
- Accent marks left by strokes running onto a printed name (`MARIA AÑGELÁ`)
  and a signature Surya reads as an italic name line are not handled.
- Surya silently corrects text that looks like a typo, so content-level tamper
  checks must not rely on the OCR text alone.
