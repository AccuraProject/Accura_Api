from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.interfaces.api.routes import register_routes
from app.infrastructure.database import initialize_database, engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa la base de datos al arrancar y libera los recursos al cerrar."""

    initialize_database()
    yield
    engine.dispose()


def create_app() -> FastAPI:
    """Crea y configura la aplicación principal de FastAPI."""

    app = FastAPI(lifespan=lifespan)

    # Autoriza peticiones desde la aplicación cliente (Angular en localhost:4200).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:4200",
            "https://accura-frontend.azurewebsites.net",
            "https://web-accura.azurewebsites.net",
            "https://web-accura-dev.azurewebsites.net", # tu frontend desplegado
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_routes(app)
    return app


app = create_app()
