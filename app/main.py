"""SafeRoute FastAPI application entrypoint."""

from pathlib import Path
from fastapi import FastAPI, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings, settings
from app.deps import get_settings

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="AI-powered Community Road-Hazard Awareness Platform",
)

# Mount static files directory
static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Configure Jinja2 templates
templates_dir = BASE_DIR / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


@app.get("/", summary="Root Welcome Endpoint")
async def root(app_settings: Settings = Depends(get_settings)):
    """Simple SafeRoute welcome response."""
    return {
        "message": f"Welcome to {app_settings.APP_NAME} - AI-powered Community Road-Hazard Awareness Platform",
        "status": "active",
        "version": app_settings.APP_VERSION,
        "docs": "/docs",
    }


@app.get("/health", summary="Health Check")
async def health_check():
    """Application health check endpoint returning system status."""
    return {
        "status": "healthy",
        "application": "SafeRoute",
    }
