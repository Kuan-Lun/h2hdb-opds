from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@asynccontextmanager
async def app_client(
    application: FastAPI,
    *,
    base_url: str = "http://testserver",
) -> AsyncIterator[AsyncClient]:
    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url=base_url,
        ) as client:
            yield client
