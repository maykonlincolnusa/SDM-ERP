"""Request diagnostics without credentials, query strings or business payloads."""

import logging
from pathlib import Path
from time import perf_counter
from traceback import extract_tb
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse


logger = logging.getLogger("uvicorn.error")


async def observe_request(request: Request, call_next):
    request_id = uuid4().hex
    request.state.request_id = request_id
    start = perf_counter()
    try:
        response = await call_next(request)
    except Exception as error:
        # Do not log exception messages: integrations may include private values.
        frames = " > ".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                            for frame in extract_tb(error.__traceback__))
        logger.error("request_id=%s exception_type=%s frames=%s", request_id, type(error).__name__, frames)
        response = JSONResponse(status_code=500, content={
            "detail": "Não foi possível concluir a operação. Consulte o histórico antes de tentar novamente.",
        })
    duration_ms = (perf_counter() - start) * 1000
    route = getattr(request.scope.get("route"), "path", "unmatched")
    if request.url.path.startswith("/api/") or response.status_code >= 500:
        log = logger.error if response.status_code >= 500 else logger.info
        log("request_id=%s method=%s route=%s status=%s duration_ms=%.2f",
            request_id, request.method, route, response.status_code, duration_ms)
    response.headers["X-Request-ID"] = request_id
    response.headers["Server-Timing"] = f"app;dur={duration_ms:.2f}"
    return response
