# LexChain

A repository for legal documents that are already notarized. A lawyer prepares a
document with their clients, uploads the signed copy, and finalizes it. Finalizing
anchors the PDF's hash on-chain, so anyone can later check that the stored copy
is the one that was signed. LexChain does not notarize.

After upload, the signed copy goes through OCR, a lawyer's review, chunking,
embedding and analysis, which powers search and Q&A over the document. The PDF,
its metadata and its approved analysis are archived to write-once storage and
anchored, so tampering with any of them is detected and can be restored.

## Layout

| Folder | What it is |
|---|---|
| [`backend/`](backend) | FastAPI API, Celery workers, Postgres + pgvector, Alembic migrations |
| [`blockchain/`](blockchain) | The Solidity contract the hashes are anchored to (Foundry) |
| [`ocr-service/`](ocr-service) | Marker OCR server for the GPU box; removes signatures and seals before reading |
| [`infra/`](infra) | OpenTofu for AWS: the app box, the GPU box, the backup buckets |

## Running locally

```bash
cd backend
# needs backend/.env (not committed; settings are in app/core/config.py)
docker compose up      # API on :8001, Postgres on :5436, Redis on :6380
```

The `migrate` service runs `alembic upgrade head` before the API starts.

## Tests

The suite runs against a real Postgres. With the compose database up:

```bash
cd backend
export TEST_DB_URL="postgresql+asyncpg://<user>:<pass>@localhost:5436/<db>_test"
pytest -q
```

## Further reading

- [`CODEBASE.md`](CODEBASE.md): how the backend is built, and the document pipeline in depth
- [`infra/README.md`](infra/README.md): environments and deploys
- [`infra/backups/RECOVERY.md`](infra/backups/RECOVERY.md): restoring from backups
- [`blockchain/README.md`](blockchain/README.md): the contract's access control and deployment
