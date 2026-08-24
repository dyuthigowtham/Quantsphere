import uuid
from pathlib import Path

import aiofiles
from fastapi import HTTPException, UploadFile, status

from config.settings import settings

_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


async def save_trade_screenshot(trade_id: int, upload_file: UploadFile) -> tuple[Path, int]:
    """
    Purpose:    Persist a user-uploaded trade screenshot to local disk
                asynchronously, enforcing content-type and size limits.
                Local disk is used deliberately to keep the AI/infra budget
                at zero — no paid object storage.
    Args:       trade_id (int): The trade this screenshot belongs to; used to
                    namespace the file path.
                upload_file (UploadFile): The incoming multipart file.
    Returns:    tuple[Path, int]: The path the file was written to, and the
                number of bytes written.
    Raises:     HTTPException: 415 if content-type is not an allowed image
                    type; 413 if the file exceeds settings.max_upload_mb.
    """
    if upload_file.content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported content type: {upload_file.content_type}",
        )

    max_bytes = settings.max_upload_mb * 1024 * 1024
    extension = Path(upload_file.filename or "").suffix or ".bin"
    target_dir = settings.screenshot_dir / str(trade_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{uuid.uuid4().hex}{extension}"

    bytes_written = 0
    async with aiofiles.open(target_path, "wb") as out_file:
        while chunk := await upload_file.read(1024 * 1024):
            bytes_written += len(chunk)
            if bytes_written > max_bytes:
                await out_file.close()
                target_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Screenshot exceeds {settings.max_upload_mb}MB limit",
                )
            await out_file.write(chunk)

    return target_path, bytes_written


async def read_screenshot_bytes(file_path: str) -> bytes:
    """
    Purpose:    Read a previously stored screenshot back into memory for
                packaging into a Phase B vision-model request.
    Args:       file_path (str): Absolute or relative path recorded on the
                    TradeScreenshot row.
    Returns:    bytes: Raw file contents.
    Raises:     FileNotFoundError: If the file no longer exists on disk.
    """
    async with aiofiles.open(file_path, "rb") as in_file:
        return await in_file.read()
