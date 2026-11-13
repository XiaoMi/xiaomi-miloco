from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from miloco.iot_observation.sidecar import Submission
from miloco.manager import get_manager
from miloco.middleware import verify_token
from miloco.schema.common_schema import NormalResponse

router = APIRouter(prefix="/iot-observation", tags=["IoT Observation"])
manager = get_manager()
logger = logging.getLogger(__name__)


class ObservationRequest(BaseModel):
    run_id: str | None = Field(
        default=None, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )


@router.post("/run", response_model=NormalResponse)
async def run_observation(
    body: ObservationRequest | None = None, _: None = Depends(verify_token)
) -> NormalResponse:
    run_id = (body.run_id if body else None) or uuid.uuid4().hex
    return NormalResponse(
        code=0,
        message="retired",
        data={"run_id": run_id, "status": "retired", "reason": "use_home_observe"},
    )


def _invoke(method, *args):
    service = manager.observation_service
    if service is None:
        return NormalResponse(code=0, message="disabled", data={"status": "disabled"})
    try:
        data = getattr(service, method)(*args)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("event=iot_observation_health reason=sidecar_unavailable")
        raise HTTPException(status_code=503, detail="observation unavailable") from exc
    return NormalResponse(code=0, message=data["status"], data=data)


@router.post("/batch", response_model=NormalResponse)
def observation_batch(_: None = Depends(verify_token)) -> NormalResponse:
    return _invoke("read_batch")


@router.post("/submit", response_model=NormalResponse)
def observation_submit(
    body: Submission, _: None = Depends(verify_token)
) -> NormalResponse:
    return _invoke("submit_batch", body)
