from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from controller import router
from service import ServiceError

app = FastAPI(title="Fawploy API", docs_url=None, redoc_url=None)
app.include_router(router)


@app.exception_handler(ServiceError)
async def service_error_handler(_request: Request, error: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content={"detail": error.message})
