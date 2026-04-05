import logging
from datetime import datetime

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

ERROR_CODE_MAP = {
    400: "VALIDATION_ERROR",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    429: "RATE_LIMIT_EXCEEDED",
    503: "SERVICE_UNAVAILABLE",
}


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = ERROR_CODE_MAP.get(exc.status_code, "INTERNAL_ERROR")
    request_id = request.headers.get("x-request-id", None)

    body = {
        "error": {
            "code": code,
            "message": str(exc.detail),
            "request_id": request_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
    }

    return JSONResponse(status_code=exc.status_code, content=body)


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(f"Unhandled exception on {request.method} {request.url.path}")
    request_id = request.headers.get("x-request-id", None)

    body = {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An unexpected error occurred",
            "request_id": request_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
    }

    return JSONResponse(status_code=500, content=body)
