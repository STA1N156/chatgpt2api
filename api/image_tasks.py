from __future__ import annotations

import json
import re

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.support import require_identity, resolve_image_base_url
from services.account_service import account_service
from services.content_filter import check_request
from services.image_task_service import image_task_service
from services.log_service import LoggedCall


class ImageGenerationTaskRequest(BaseModel):
    client_task_id: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    size: str | None = None


class PublicImageGenerationTaskRequest(BaseModel):
    client_task_ids: list[str] = Field(..., min_length=1, max_length=6)
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    size: str | None = None


PUBLIC_MAX_IMAGE_COUNT = 6
PUBLIC_USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def _parse_task_ids(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_public_task_ids(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    try:
        raw = json.loads(text)
    except Exception:
        raw = None
    if isinstance(raw, list):
        ids = [str(item).strip() for item in raw if str(item).strip()]
    else:
        ids = _parse_task_ids(text)
    return list(dict.fromkeys(ids))


def _public_identity(user_id: str | None) -> dict[str, object]:
    value = str(user_id or "").strip()
    if not PUBLIC_USER_ID_PATTERN.match(value):
        raise HTTPException(status_code=400, detail={"error": "invalid public user id"})
    return {"id": f"public:{value}", "name": "public user", "role": "public"}


def _validate_public_task_ids(task_ids: list[str]) -> list[str]:
    ids = [str(item).strip() for item in task_ids if str(item).strip()]
    if not ids:
        raise HTTPException(status_code=400, detail={"error": "client_task_ids is required"})
    if len(ids) > PUBLIC_MAX_IMAGE_COUNT:
        raise HTTPException(status_code=400, detail={"error": f"single request can create at most {PUBLIC_MAX_IMAGE_COUNT} images"})
    return ids


def _public_image_quota() -> dict[str, int]:
    accounts = account_service.list_accounts()
    available_accounts = [
        account
        for account in accounts
        if str(account.get("status") or "") not in {"禁用", "异常"}
    ]
    quota = sum(max(0, int(account.get("quota") or 0)) for account in available_accounts)
    return {"quota": quota, "available": len(available_accounts)}


async def filter_or_log(call: LoggedCall, text: str) -> None:
    try:
        await run_in_threadpool(check_request, text)
    except HTTPException as exc:
        call.log("调用失败", status="failed", error=str(exc.detail))
        raise


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/image-tasks")
    async def list_image_tasks(
        ids: str = Query(default=""),
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        return await run_in_threadpool(image_task_service.list_tasks, identity, _parse_task_ids(ids))

    @router.post("/api/image-tasks/generations")
    async def create_generation_task(
        body: ImageGenerationTaskRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        await filter_or_log(LoggedCall(identity, "/api/image-tasks/generations", body.model, "文生图任务", request_text=body.prompt), body.prompt)
        try:
            return await run_in_threadpool(
                image_task_service.submit_generation,
                identity,
                client_task_id=body.client_task_id,
                prompt=body.prompt,
                model=body.model,
                size=body.size,
                base_url=resolve_image_base_url(request),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/image-tasks/edits")
    async def create_edit_task(
        request: Request,
        authorization: str | None = Header(default=None),
        image: list[UploadFile] | None = File(default=None),
        image_list: list[UploadFile] | None = File(default=None, alias="image[]"),
        client_task_id: str = Form(...),
        prompt: str = Form(...),
        model: str = Form(default="gpt-image-2"),
        size: str | None = Form(default=None),
    ):
        identity = require_identity(authorization)
        await filter_or_log(LoggedCall(identity, "/api/image-tasks/edits", model, "图生图任务", request_text=prompt), prompt)
        uploads = [*(image or []), *(image_list or [])]
        if not uploads:
            raise HTTPException(status_code=400, detail={"error": "image file is required"})
        images: list[tuple[bytes, str, str]] = []
        for upload in uploads:
            image_data = await upload.read()
            if not image_data:
                raise HTTPException(status_code=400, detail={"error": "image file is empty"})
            images.append((image_data, upload.filename or "image.png", upload.content_type or "image/png"))
        try:
            return await run_in_threadpool(
                image_task_service.submit_edit,
                identity,
                client_task_id=client_task_id,
                prompt=prompt,
                model=model,
                size=size,
                base_url=resolve_image_base_url(request),
                images=images,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.get("/api/public/image-tasks")
    async def list_public_image_tasks(
        ids: str = Query(default=""),
        x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    ):
        identity = _public_identity(x_user_id)
        return await run_in_threadpool(image_task_service.list_tasks, identity, _parse_task_ids(ids))

    @router.get("/api/public/image-quota")
    async def get_public_image_quota():
        return await run_in_threadpool(_public_image_quota)

    @router.post("/api/public/image-tasks/generations")
    async def create_public_generation_tasks(
        body: PublicImageGenerationTaskRequest,
        request: Request,
        x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    ):
        identity = _public_identity(x_user_id)
        task_ids = _validate_public_task_ids(body.client_task_ids)
        await filter_or_log(LoggedCall(identity, "/api/public/image-tasks/generations", body.model, "公开文生图任务", request_text=body.prompt), body.prompt)
        try:
            items = [
                image_task_service.submit_generation(
                    identity,
                    client_task_id=task_id,
                    prompt=body.prompt,
                    model=body.model,
                    size=body.size,
                    base_url=resolve_image_base_url(request),
                )
                for task_id in task_ids
            ]
            return {"items": items}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/public/image-tasks/edits")
    async def create_public_edit_tasks(
        request: Request,
        x_user_id: str | None = Header(default=None, alias="X-User-Id"),
        image: list[UploadFile] | None = File(default=None),
        image_list: list[UploadFile] | None = File(default=None, alias="image[]"),
        client_task_ids: str = Form(...),
        prompt: str = Form(...),
        model: str = Form(default="gpt-image-2"),
        size: str | None = Form(default=None),
    ):
        identity = _public_identity(x_user_id)
        task_ids = _validate_public_task_ids(_parse_public_task_ids(client_task_ids))
        await filter_or_log(LoggedCall(identity, "/api/public/image-tasks/edits", model, "公开图生图任务", request_text=prompt), prompt)
        uploads = [*(image or []), *(image_list or [])]
        if not uploads:
            raise HTTPException(status_code=400, detail={"error": "image file is required"})
        images: list[tuple[bytes, str, str]] = []
        for upload in uploads:
            image_data = await upload.read()
            if not image_data:
                raise HTTPException(status_code=400, detail={"error": "image file is empty"})
            images.append((image_data, upload.filename or "image.png", upload.content_type or "image/png"))
        try:
            items = [
                image_task_service.submit_edit(
                    identity,
                    client_task_id=task_id,
                    prompt=prompt,
                    model=model,
                    size=size,
                    base_url=resolve_image_base_url(request),
                    images=images,
                )
                for task_id in task_ids
            ]
            return {"items": items}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    return router
