from fastapi import FastAPI

from api.routes import create_fastapi_app
from config.config import Config
from config.logger import setup_logging
from services.storage import CloudflareR2, R2Storage

def create_app() -> FastAPI:
    config = Config()
    config.validate_for_production()

    setup_logging(config.server.debug)

    storage = CloudflareR2(
        config=R2Storage(
            account_id=config.cloudflare_account_id,
            bucket_name=config.r2_storage.bucket_name,
            public_domain=config.r2_storage.public_domain,
            access_key_id=config.r2_storage.access_key_id,
            secret_access_key=config.r2_storage.secret_access_key,
        )
    )

    app = create_fastapi_app(config, storage)

    return app
