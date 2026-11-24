import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from app.api.routes import router as api_router
from app.config import settings
from app.database import init_db

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize database tables
    init_db()
    logging.info("SQLite database tables initialized successfully.")
    yield
    # Shutdown
    logging.info("Application shutting down.")


app = FastAPI(
    title="GitHub PR Reviewer & Code-Quality API",
    description="""
A backend-heavy developer tool that inspects GitHub pull requests or unified code diffs,
executes deterministic static analysis alongside an LLM pass, and returns structured findings
categorized by bugs, security/edge cases, performance, and maintainability.
    """,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API Router
app.include_router(api_router)


@app.get("/", include_in_schema=False)
def root_redirect():
    """Redirect root path to interactive Swagger documentation."""
    return RedirectResponse(url="/docs")

