from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import seed
from app.routers import api


@asynccontextmanager
async def lifespan(_app: FastAPI):
    seed.init_db()
    yield


app = FastAPI(title="点跨 Clickspan", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(api)


@app.get("/api/health")
def health():
    return {"正常": True, "项目": "点跨", "slug": "clickspan"}
