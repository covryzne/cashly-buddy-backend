from typing import Literal

from pydantic import BaseModel


class HealthData(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["cashly-api"] = "cashly-api"


class HealthEnvelope(BaseModel):
    data: HealthData
