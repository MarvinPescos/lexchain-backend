from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.features.auth import router as auth_router
from app.features.documents import router as document_router
from app.features.retrieval import router as retrieval_router
from app.features.blockchain import router as blockchain_router

from app.shared.errors.exceptions import BaseAppException


app = FastAPI()


@app.exception_handler(BaseAppException)
async def app_exception_handler(request: Request, exc: BaseAppException):
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_dict()
    )

app.include_router(auth_router)
app.include_router(document_router)
app.include_router(retrieval_router)
app.include_router(blockchain_router)


