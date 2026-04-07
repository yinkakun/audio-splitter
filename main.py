import uvicorn

from app import create_app
from config.config import Config

app = create_app()


if __name__ == "__main__":
    config = Config()

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=config.port,
        reload=config.debug,
        log_level="info",
        access_log=True,
    )
