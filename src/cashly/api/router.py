from fastapi import APIRouter

from cashly.api.accounts import router as accounts_router
from cashly.api.auth import router as auth_router
from cashly.api.chat import router as chat_router
from cashly.api.finance import router as finance_router
from cashly.api.profile import router as profile_router
from cashly.schemas.health import HealthData, HealthEnvelope

router = APIRouter(prefix="/api/v1")
router.include_router(accounts_router)
router.include_router(auth_router)
router.include_router(chat_router)
router.include_router(finance_router)
router.include_router(profile_router)


@router.get("/health", response_model=HealthEnvelope, tags=["Operations"])
async def health() -> HealthEnvelope:
    """Process liveness only; does not verify database or provider readiness."""
    return HealthEnvelope(data=HealthData())
