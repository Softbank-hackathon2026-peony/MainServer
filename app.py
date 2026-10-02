from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from controller import router
from config import CORS_ORIGINS
from service import ServiceError

app = FastAPI(title="Fawploy API", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-Project-Token"])
app.include_router(router)


@app.exception_handler(ServiceError)
async def service_error_handler(_request: Request, error: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content={"detail": error.message})
