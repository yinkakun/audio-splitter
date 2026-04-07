import asyncio
import mimetypes
from pathlib import Path

import aioboto3
import boto3
from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError, NoCredentialsError
from pydantic import BaseModel

from config.logger import get_logger
from services.audio_classifier import AudioClassifier

logger = get_logger(__name__)


def get_storage_client(
    account_id: str,
    access_key_id: str,
    secret_access_key: str,
    bucket_name: str,
    public_domain: str,
) -> "CloudflareR2":
    """create storage client"""
    config = R2Storage(
        account_id=account_id,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        bucket_name=bucket_name,
        public_domain=public_domain,
    )
    return CloudflareR2(config)


class R2Storage(BaseModel):
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket_name: str
    public_domain: str
    region: str = "auto"
    max_pool_connections: int = 10


class CloudflareR2:

    def __init__(
        self,
        config: R2Storage,
    ):
        self.endpoint_url = f"https://{config.account_id}.r2.cloudflarestorage.com"
        self.access_key_id = config.access_key_id
        self.secret_access_key = config.secret_access_key
        self.bucket_name = config.bucket_name
        self.public_domain = config.public_domain
        self.region = config.region

        self.client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            region_name=self.region,
        )

        self._session: aioboto3.Session
        self._config: AioConfig
        self._init_async_config(config)

    def _init_async_config(self, config: R2Storage) -> None:
        try:
            self._config = AioConfig(
                max_pool_connections=config.max_pool_connections,
                retries={"max_attempts": 3, "mode": "adaptive"},
            )
            self._session = aioboto3.Session()
            logger.info("Initialized R2")
        except (ValueError, OSError, RuntimeError) as e:
            logger.error("Failed to initialize R2: %s", str(e))
            self._config = AioConfig(retries={"max_attempts": 3, "mode": "adaptive"})
            self._session = aioboto3.Session()

    async def close(self) -> None:
        logger.debug("R2Storage closed")

    @staticmethod
    def _get_content_type(file_path: Path) -> str:
        content_type, _ = mimetypes.guess_type(file_path.name)
        return content_type or "application/octet-stream"

    def upload_file(self, file_path: Path, key: str) -> bool:
        try:
            self.client.upload_file(
                str(file_path),
                self.bucket_name,
                key,
                ExtraArgs={"ContentType": self._get_content_type(file_path)},
            )
            logger.info("Uploaded %s to R2 as %s", file_path, key)
            return True
        except (NoCredentialsError, ClientError, OSError, FileNotFoundError) as e:
            logger.error("R2 upload error: %s", str(e))
            return False

    def file_exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except ClientError:
            return False

    def delete_file(self, key: str) -> bool:
        try:
            self.client.delete_object(Bucket=self.bucket_name, Key=key)
            return True
        except (NoCredentialsError, ClientError) as e:
            logger.error("R2 delete error: %s", str(e))
            return False

    def get_download_url(self, key: str, public_domain: str = "") -> str | None:
        if not public_domain:
            return None
        url = f"https://{public_domain}/{key}"
        return url

    async def upload_file_async(self, file_path: Path, key: str) -> bool:
        try:
            async with self._session.client(  # type: ignore[attr-defined]
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region,
                config=self._config,
            ) as client:
                await client.upload_file(
                    str(file_path),
                    self.bucket_name,
                    key,
                    ExtraArgs={"ContentType": self._get_content_type(file_path)},
                )
                logger.info("Uploaded %s to R2 as %s (async)", file_path, key)
                return True
        except (NoCredentialsError, ClientError, OSError, FileNotFoundError) as e:
            logger.error("R2 async upload error: %s", str(e))
            return False

    async def upload_files_parallel(self, file_uploads: list[tuple[Path, str]]) -> list[bool]:
        upload_tasks = [self.upload_file_async(file_path, key) for file_path, key in file_uploads]
        results = await asyncio.gather(*upload_tasks, return_exceptions=True)

        final_results = []
        for result in results:
            if isinstance(result, Exception):
                logger.error("Parallel upload exception: %s", str(result))
                final_results.append(False)
            else:
                final_results.append(result)

        return final_results

    async def file_exists_async(self, key: str) -> bool:
        try:
            async with self._session.client(  # type: ignore[attr-defined]
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region,
                config=self._config,
            ) as client:
                await client.head_object(Bucket=self.bucket_name, Key=key)
                return True
        except ClientError:
            return False

    def cleanup_track_files(self, track_id: str):
        try:
            response = self.client.list_objects_v2(Bucket=self.bucket_name, Prefix=f"{track_id}/")
            for item in response.get("Contents", []):
                r2_key = item["Key"]
                try:
                    if self.delete_file(r2_key):
                        logger.debug("Cleaned up R2 file: %s", r2_key)
                except (ClientError, NoCredentialsError, OSError) as e:
                    logger.warning("Failed to clean up R2 file %s: %s", r2_key, e)
        except (TypeError, ValueError, AttributeError, ClientError, NoCredentialsError) as e:
            logger.error("Unexpected error during cleanup for %s: %s", track_id, e)

    @staticmethod
    def detect_stem_files(output_dir: Path) -> dict[str, Path]:
        return AudioClassifier.detect_stem_files(output_dir)

    def _rollback_uploaded_keys(self, uploaded_keys: dict[str, str]) -> None:
        """Delete already-uploaded files during a failed batch upload."""
        for stem_name, stem_key in uploaded_keys.items():
            try:
                if self.delete_file(stem_key):
                    logger.debug("Rolled back uploaded file: %s", stem_key)
            except (ClientError, NoCredentialsError, OSError) as e:
                logger.warning("Failed to rollback file %s: %s", stem_key, e)

    def upload_stem_files(self, track_id: str, stem_files: dict[str, Path]) -> dict[str, str]:
        uploaded_keys: dict[str, str] = {}
        try:
            for stem_name, stem_file in stem_files.items():
                stem_key = f"{track_id}/stems/{stem_name}{stem_file.suffix.lower()}"
                if not self.upload_file(stem_file, stem_key):
                    logger.error("Upload failed for %s, rolling back %d files", stem_key, len(uploaded_keys))
                    self._rollback_uploaded_keys(uploaded_keys)
                    return {}
                uploaded_keys[stem_name] = stem_key
            return uploaded_keys
        except (ClientError, NoCredentialsError) as e:
            logger.error("Failed to upload stem files to R2: %s", e)
            self._rollback_uploaded_keys(uploaded_keys)
            return {}

    async def upload_stem_files_async(self, track_id: str, stem_files: dict[str, Path]) -> dict[str, str]:
        try:
            file_uploads = [
                (stem_file, f"{track_id}/stems/{stem_name}{stem_file.suffix.lower()}")
                for stem_name, stem_file in stem_files.items()
            ]

            upload_results = await self.upload_files_parallel(file_uploads)
            uploaded_keys = {
                stem_name: f"{track_id}/stems/{stem_name}{stem_file.suffix.lower()}"
                for stem_name, stem_file in stem_files.items()
            }
            if not all(upload_results):
                # Rollback successfully uploaded files
                successful_keys = {
                    stem_name: key
                    for (stem_name, _), success, key in zip(
                        stem_files.items(), upload_results, uploaded_keys.values()
                    )
                    if success
                }
                logger.error(
                    "Partial upload failure, rolling back %d files", len(successful_keys)
                )
                self._rollback_uploaded_keys(successful_keys)
                return {}
            return uploaded_keys

        except (ClientError, NoCredentialsError) as e:
            logger.error("Failed to upload track files to R2 (async): %s", e)
            return {}
