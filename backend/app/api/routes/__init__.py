"""API 路由聚合。"""

from fastapi import APIRouter

from app.api.routes import auth, documents, kbs

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(kbs.router)
api_router.include_router(documents.router)

__all__ = ["api_router"]
