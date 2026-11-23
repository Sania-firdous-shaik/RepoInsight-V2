
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.db.session import Base, engine
from app.models import models
from app.api.routes import router
from app.services.vector_store.qdrant import client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("Starting RepoInsight API")

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        logging.info("PostgreSQL connection successful")
    except Exception:
        logging.exception("PostgreSQL connection failed")

    try:
        client.get_collections()
        logging.info("Qdrant connection successful")
    except Exception:
        logging.exception("Qdrant connection failed")

    yield

    logging.info("Shutting down RepoInsight API")


app = FastAPI(
    title="RepoInsight API",
    version="2.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.exception_handler(SQLAlchemyError)
async def database_error_handler(request: Request, exc: SQLAlchemyError):
    logging.getLogger(__name__).error(
        "Database request failed path=%s", request.url.path,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(
        status_code=503,
        content={"detail": "Repository database is unavailable. Check PostgreSQL and backend logs."},
    )


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "application": "RepoInsight API",
        "version": "2.0.0"
    }

