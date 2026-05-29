from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import app.all_models  # Ensure all SQLAlchemy models are registered at startup

from app.features.admin import router as admin_router
from app.features.auth import router as auth_router
from app.features.blockchain import router as blockchain_router
from app.features.documents import router as document_router
from app.features.notifications import router as notification_router
from app.features.public import router as public_router
from app.features.retrieval import router as retrieval_router
from app.features.users import router as user_router
from app.shared.errors.exceptions import BaseAppException

app = FastAPI()


@app.exception_handler(BaseAppException)
async def app_exception_handler(request: Request, exc: BaseAppException):
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


app.include_router(auth_router)
app.include_router(user_router)
app.include_router(document_router)
app.include_router(retrieval_router)
app.include_router(blockchain_router)
app.include_router(admin_router)
app.include_router(public_router)
app.include_router(notification_router)
