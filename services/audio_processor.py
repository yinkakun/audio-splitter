import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Optional, Protocol, Required, TypedDict, cast

import yt_dlp
from audio_separator.separator import Separator
from yt_dlp.utils import DownloadError, ExtractorError

from config.logger import get_logger
from services.audio_classifier import AudioClassifier
logger = get_logger(__name__)

MODEL_FILENAME = "htdemucs_ft.yaml"


class MdxParams(TypedDict):
    hop_length: int
    segment_size: int
    overlap: float
    batch_size: int
    enable_denoise: bool


class YoutubeDLOpts(TypedDict, total=False):
    quiet: bool
    no_warnings: bool
    format: str
    outtmpl: str


class YtDlpEntry(TypedDict, total=False):
    title: str
    filesize: int | None
    filesize_approx: int | None


class YtDlpSearchResult(TypedDict, total=False):
    entries: Required[list[YtDlpEntry]]


class AudioProcessResult(TypedDict, total=False):
    original_title: str
    track_id: str
    stems_urls: dict[str, str]
    storage: str
    error: str


mdx_params: MdxParams = {
    "hop_length": 1024,
    "segment_size": 256,
    "overlap": 0.25,
    "batch_size": 1,
    "enable_denoise": False,
}


class SeparatorProvider(Protocol):
    def get_separator(self) -> Separator: ...

    def initialize_model(self) -> None: ...

    @property
    def separator_lock(self) -> threading.RLock: ...


class DefaultSeparatorProvider:
    def __init__(self, models_dir: str):
        self._separator: Optional[Separator] = None
        # RLock needed: separate_and_move acquires lock then calls get_separator which may also acquire
        self._separator_lock = threading.RLock()
        self.model_load_timeout = 60 * 15  # 15 minutes
        self.working_dir = Path("audio_workspace")
        self.models_dir = models_dir

    def initialize_model(self) -> None:
        with self._separator_lock:
            if self._separator is not None:
                return

            try:
                self._separator = self._create_separator()
                self._separator.load_model(model_filename=MODEL_FILENAME)
                logger.info("Separator model preloaded successfully", model=MODEL_FILENAME)
            except (ImportError, RuntimeError, OSError) as e:
                logger.error(
                    "Failed to preload separator model on startup",
                    error=str(e),
                    model=MODEL_FILENAME,
                    models_dir=self.models_dir,
                )
                self._separator = None
                raise RuntimeError(f"Cannot preload audio separator: {str(e)}") from e

    def _create_separator(self) -> Separator:
        models_path = Path(self.models_dir)
        models_path.mkdir(parents=True, exist_ok=True)

        separator_config = {
            "output_dir": str(self.working_dir.resolve()),
            "output_format": "WAV",
            "output_bitrate": "320k",
            "normalization_threshold": 0.9,
            "amplification_threshold": 0.9,
            "mdx_params": mdx_params,
            "model_file_dir": self.models_dir,
        }

        try:
            separator = Separator(**separator_config)
            logger.info("Separator created successfully with config", config=separator_config)
            return separator
        except Exception as e:
            logger.error("Failed to create separator", error=str(e), config=separator_config)
            raise

    def get_separator(self) -> Separator:
        if self._separator is not None:
            return self._separator

        with self._separator_lock:
            if self._separator is not None:
                return self._separator

            try:
                self._separator = self._create_separator()
                self._separator.load_model(model_filename=MODEL_FILENAME)
                return self._separator
            except (ImportError, RuntimeError, OSError) as e:
                self._separator = None
                raise RuntimeError(f"Cannot initialize audio separator: {str(e)}") from e

    @property
    def separator_lock(self) -> threading.RLock:
        return self._separator_lock

    def separate_and_move(
        self, audio_file: Path, target_dir: Path, timeout: int = 300
    ) -> list[Path]:
        """
        Separate audio using the singleton separator and move results to target directory.

        The separator always writes to its configured output_dir (working_dir).
        We then move the output files to the caller's target directory.
        This avoids relying on internal APIs to change the output directory dynamically.

        Thread-safe: acquires the separator lock internally.
        """
        with self._separator_lock:
            separator = self.get_separator()

            # Separator writes files to self.working_dir
            output_files = run_with_timeout(separator.separate, str(audio_file), timeout=timeout)
            logger.debug("Separation produced files", output_files=output_files)

            # Move files from separator's output_dir to target_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            result_paths: list[Path] = []

            for output_file_str in output_files:
                src = Path(output_file_str)
                if not src.exists():
                    logger.warning("Separated file not found at expected path", path=str(src))
                    continue

                dest = target_dir / src.name
                shutil.move(str(src), str(dest))
                result_paths.append(dest)
                logger.debug("Moved separated file", src=str(src), dest=str(dest))

            # Clean up any subdirectories created by the separator in working_dir
            # (htdemucs creates a subdirectory named after the input file)
            for item in self.working_dir.iterdir():
                if item.is_dir() and item != target_dir:
                    shutil.rmtree(item, ignore_errors=True)

            return result_paths


def run_with_timeout(fn, *args, timeout: int = 300):
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn, *args)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeout as exc:
            future.cancel()
            raise TimeoutError(f"Operation timed out after {timeout} seconds") from exc


def retry_with_backoff(func, max_retries=3, base_delay=1, backoff_factor=2, max_delay=60):
    """Retry function with exponential backoff."""
    for attempt in range(max_retries):
        try:
            return func()
        except (OSError, RuntimeError) as e:
            if attempt == max_retries - 1:
                raise e

            delay = min(base_delay * (backoff_factor**attempt), max_delay)
            logger.warning(
                "Attempt failed, retrying",
                attempt=attempt + 1,
                max_retries=max_retries,
                delay=delay,
                error=str(e),
            )
            time.sleep(delay)


class AudioProcessor:

    def __init__(
        self,
        storage,
        models_dir: str,
        working_dir: str,
        separator_provider: Optional[DefaultSeparatorProvider] = None,
    ):
        self.storage = storage
        self.working_dir = Path(working_dir)
        self.separator_provider = separator_provider or DefaultSeparatorProvider(
            models_dir=models_dir
        )
        self._ensure_working_dir()

    def _ensure_working_dir(self) -> None:
        """Ensure the working directory exists."""
        self.working_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Working directory initialized", path=str(self.working_dir))

    def cleanup_working_dir(self) -> None:
        if self.working_dir.exists():
            shutil.rmtree(self.working_dir, ignore_errors=True)
            logger.info("Working directory cleaned up", path=str(self.working_dir))
            self._ensure_working_dir()

    def initialize_model(self) -> None:
        self.separator_provider.initialize_model()

    def _validate_file_size(self, size_bytes: int, max_file_size_mb: int = 100) -> None:
        size_mb = int(size_bytes) / (1024 * 1024)
        if size_mb > max_file_size_mb:
            raise ValueError(f"File too large: {size_mb:.1f}MB (limit: {max_file_size_mb}MB)")

    def _validate_file_size_approx(self, size_bytes: int, max_file_size_mb: int = 100) -> None:
        size_mb = int(size_bytes) / (1024 * 1024)
        if size_mb > max_file_size_mb:
            raise ValueError(
                f"File too large (approx): {size_mb:.1f}MB (limit: {max_file_size_mb}MB)"
            )

    def _validate_audio_file(self, audio_file: Path) -> None:
        if not audio_file.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_file}")

        if audio_file.stat().st_size == 0:
            raise ValueError(f"Audio file is empty: {audio_file}")

        allowed_extensions = {".m4a", ".mp3", ".wav", ".flac", ".aac", ".ogg"}
        if audio_file.suffix.lower() not in allowed_extensions:
            raise ValueError(f"Unsupported audio format: {audio_file.suffix}")

    def _validate_separated_files(self, output_dir: Path, track_id: str = "") -> dict[str, Path]:
        stem_files = self.storage.detect_stem_files(output_dir)
        is_valid, missing_stems = AudioClassifier.validate_required_stems(stem_files)
        if is_valid:
            return stem_files
        available_files = [
            str(file_path.relative_to(output_dir))
            for file_path in sorted(output_dir.rglob("*"))
            if file_path.is_file()
        ]
        if track_id:
            logger.error(
                "Could not find separated tracks after separation",
                track_id=track_id,
                available_files=available_files,
                detected_stems=sorted(stem_files),
                missing_stems=sorted(missing_stems),
                output_dir=str(output_dir),
            )
        raise FileNotFoundError(
            "Could not find required separated tracks. "
            f"Missing stems: {sorted(missing_stems) or sorted(AudioClassifier.REQUIRED_STEMS)}. "
            f"Found files: {available_files}"
        )

    def _setup_job_directory(self, track_id: str) -> Path:
        job_dir = self.working_dir / track_id
        job_dir.mkdir(parents=True, exist_ok=True)
        return job_dir

    def _process_audio_files(
        self, job_dir: Path, track_id: str, search_query: str, max_file_size_mb: int
    ) -> tuple[dict[str, Path], str]:
        logger.info("Downloading audio", track_id=track_id, query=search_query)
        downloaded_file, original_title = self.download_audio(
            job_dir, search_query, max_file_size_mb
        )

        logger.info("Separating audio", track_id=track_id)
        stem_files = self.separate_audio_tracks(job_dir, downloaded_file, track_id)

        return stem_files, original_title or ""

    def _upload_and_create_result(
        self, track_id: str, stem_files: dict[str, Path], original_title: str
    ) -> AudioProcessResult:
        logger.info("Uploading separated stem files", track_id=track_id, stem_count=len(stem_files))
        uploaded_keys = self.storage.upload_stem_files(track_id, stem_files)

        if not uploaded_keys:
            raise RuntimeError("Failed to upload separated stem files to storage")

        logger.info("Upload successful", track_id=track_id)
        return self.create_result_dict(track_id, original_title, uploaded_keys)

    def _cleanup_job_directory(self, job_dir: Optional[Path]) -> None:
        if job_dir and job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)

    def process_audio(
        self,
        track_id: str,
        search_query: str,
        max_file_size_mb: int,
    ) -> AudioProcessResult:
        job_dir: Optional[Path] = None

        try:
            job_dir = self._setup_job_directory(track_id)

            stem_files, original_title = self._process_audio_files(
                job_dir, track_id, search_query, max_file_size_mb
            )

            result = self._upload_and_create_result(track_id, stem_files, original_title)

            logger.info(
                "Job completed successfully", track_id=track_id, result_keys=list(result.keys())
            )
            return result

        except (RuntimeError, OSError, FileNotFoundError, ValueError, TimeoutError) as e:
            error_msg = str(e)
            logger.error(
                "Audio processing failed", track_id=track_id, error=error_msg, query=search_query
            )
            logger.error("Job failed", track_id=track_id, error=error_msg)
            self.storage.cleanup_track_files(track_id)
            raise

        finally:
            self._cleanup_job_directory(job_dir)

    def create_result_dict(
        self, track_id: str, original_title: Optional[str], uploaded_keys: dict[str, str]
    ) -> AudioProcessResult:
        result: AudioProcessResult = {
            "original_title": original_title or "Unknown Title",
            "track_id": track_id,
        }

        stem_urls = {
            stem_name: self.storage.get_download_url(storage_key, self.storage.public_domain)
            for stem_name, storage_key in uploaded_keys.items()
        }

        if not stem_urls or any(not url for url in stem_urls.values()):
            result["error"] = "Failed to generate download URLs from R2 storage"
            result["storage"] = "r2_failed"
            return result

        result["stems_urls"] = stem_urls
        result["storage"] = "r2"
        return result

    def _get_base_ydl_opts(self) -> YoutubeDLOpts:
        opts: YoutubeDLOpts = {
            "quiet": True,
            "no_warnings": True,
        }
        return opts

    def _validate_search_results(self, search_results: YtDlpSearchResult) -> YtDlpEntry:
        entries = search_results.get("entries")
        if not entries:
            raise RuntimeError("No search results found")
        return entries[0]

    def _validate_entry_file_size(self, entry: YtDlpEntry, max_file_size_mb: int) -> None:
        filesize = entry.get("filesize")
        if filesize:
            self._validate_file_size(filesize, max_file_size_mb)
            return

        filesize_approx = entry.get("filesize_approx")
        if filesize_approx:
            self._validate_file_size_approx(filesize_approx, max_file_size_mb)

    def _extract_title_from_entry(self, entry: YtDlpEntry) -> str:
        title_value = entry.get("title")
        return title_value if isinstance(title_value, str) else "Unknown Title"

    def download_audio(
        self, job_dir: Path, search_query: str, max_file_size_mb: int
    ) -> tuple[Path, Optional[str]]:
        def _download():
            search_term = f"ytsearch1:{search_query}"
            extract_opts: YoutubeDLOpts = {
                **self._get_base_ydl_opts(),
                "format": "bestaudio/best",
            }
            with yt_dlp.YoutubeDL(extract_opts) as ydl:  # pyright: ignore[reportArgumentType]
                search_results = ydl.extract_info(search_term, download=False)
                if search_results is None:
                    raise RuntimeError("No search results found")
                first_entry = self._validate_search_results(
                    cast(YtDlpSearchResult, search_results)
                )
                self._validate_entry_file_size(first_entry, max_file_size_mb)

            download_opts: YoutubeDLOpts = {
                **self._get_base_ydl_opts(),
                "format": "bestaudio[ext=m4a]/bestaudio",
                "outtmpl": str(job_dir / "original.%(ext)s"),
            }
            with yt_dlp.YoutubeDL(download_opts) as ydl:  # pyright: ignore[reportArgumentType]
                search_results = ydl.extract_info(search_term, download=True)
                if search_results is None:
                    raise RuntimeError("No search results found")
                first_entry = self._validate_search_results(
                    cast(YtDlpSearchResult, search_results)
                )

                for file_path in job_dir.iterdir():
                    if file_path.name.startswith("original."):
                        self._validate_file_size(file_path.stat().st_size, max_file_size_mb)
                        original_title = self._extract_title_from_entry(first_entry)
                        return file_path, original_title

                raise RuntimeError("Downloaded file not found")

        try:
            return run_with_timeout(_download, timeout=300)
        except (DownloadError, ExtractorError) as e:
            raise RuntimeError(f"YouTube download failed: {str(e)}") from e
        except (OSError, IOError) as e:
            raise RuntimeError(f"File system error during download: {str(e)}") from e
        except (KeyError, TypeError, ValueError) as e:
            raise RuntimeError(f"Invalid response data from YouTube: {str(e)}") from e

    def separate_audio_tracks(
        self,
        job_dir: Path,
        audio_file: Path,
        track_id: str,
        processing_timeout: int = 300,
    ) -> dict[str, Path]:
        output_dir = job_dir / "separated"
        output_dir.mkdir(exist_ok=True, parents=True)

        try:
            self._validate_audio_file(audio_file)

            logger.debug(
                "Separator configuration",
                track_id=track_id,
                output_dir=output_dir,
                model=MODEL_FILENAME,
                models_dir=self.separator_provider.models_dir,
            )

            try:
                output_files = self.separator_provider.separate_and_move(
                    audio_file, output_dir, timeout=processing_timeout
                )
                logger.info(
                    "Separation completed", track_id=track_id, output_files=output_files
                )
            except Exception as sep_error:
                logger.error(
                    "Separation process failed",
                    track_id=track_id,
                    error=str(sep_error),
                    error_type=type(sep_error).__name__,
                )
                raise

        except (RuntimeError, OSError, TimeoutError, ValueError, FileNotFoundError) as e:
            logger.error(
                "Failed to process file",
                track_id=track_id,
                audio_file_path=str(audio_file),
                error=str(e),
                error_type=type(e).__name__,
            )
            raise RuntimeError(f"Audio separation failed: {str(e)}") from e

        return self._validate_separated_files(output_dir, track_id)

    def separate_audio(self, audio_file: Path) -> dict[str, Path]:
        output_dir = audio_file.parent / "separated"
        output_dir.mkdir(exist_ok=True, parents=True)

        try:
            self._validate_audio_file(audio_file)
            self.separator_provider.separate_and_move(audio_file, output_dir)

            return self._validate_separated_files(output_dir)

        except Exception as e:
            raise RuntimeError(f"Audio separation failed: {str(e)}") from e
