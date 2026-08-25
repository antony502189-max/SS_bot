from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db import engine
from .models import Base
from .routers import admin, auth, events, reports, sectors, tasks, users


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Local development convenience. Production containers use `alembic upgrade head`.
    if get_settings().app_env == "development" and get_settings().database_url.startswith("sqlite"):
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(title="SS Bot API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(sectors.router, prefix="/api/v1")
app.include_router(events.router, prefix="/api/v1")
app.include_router(tasks.router, prefix="/api/v1")
app.include_router(reports.router, prefix="/api/v1")


@app.get("/healthz", tags=["operations"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
