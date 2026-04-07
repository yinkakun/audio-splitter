from pathlib import Path
from typing import Dict, List

from config.logger import get_logger

logger = get_logger(__name__)

class AudioClassifier:
    REQUIRED_STEMS = {"vocals", "bass", "drums", "other"}
    STEM_PATTERNS: Dict[str, List[str]] = {
        "vocals": ["vocals", "(vocals)"],
        "bass": ["bass", "(bass)"],
        "drums": ["drums", "(drums)"],
        "other": ["other", "(other)"],
        "guitar": ["guitar", "(guitar)"],
        "piano": ["piano", "(piano)"],
        "synthesizer": ["synthesizer", "synth", "(synthesizer)", "(synth)"],
        "strings": ["strings", "(strings)"],
        "woodwinds": ["woodwinds", "(woodwinds)"],
        "brass": ["brass", "(brass)"],
        "wind_inst": ["wind inst", "wind_inst", "(wind inst)", "(wind_inst)"],
    }

    @classmethod
    def _iter_audio_files(cls, output_dir: Path) -> List[Path]:
        return sorted(
            file_path for file_path in output_dir.rglob("*") if file_path.is_file()
        )

    @classmethod
    def _matches_patterns(cls, file_path: Path, patterns: List[str]) -> bool:
        stem_lower = file_path.stem.lower()
        return any(pattern.lower() in stem_lower for pattern in patterns)

    @classmethod
    def detect_stem_files(cls, output_dir: Path) -> dict[str, Path]:
        stem_files: dict[str, Path] = {}
        for file_path in cls._iter_audio_files(output_dir):
            matched_stem: str | None = None
            for stem_name, patterns in cls.STEM_PATTERNS.items():
                if stem_name in stem_files:
                    continue
                if cls._matches_patterns(file_path, patterns):
                    if matched_stem is not None:
                        logger.warning(
                            "Ambiguous stem match for %s: matches both '%s' and '%s', using '%s'",
                            file_path.name,
                            matched_stem,
                            stem_name,
                            matched_stem,
                        )
                        break
                    matched_stem = stem_name
            if matched_stem is not None:
                stem_files[matched_stem] = file_path
        return stem_files

    @classmethod
    def validate_required_stems(cls, stem_files: dict[str, Path]) -> tuple[bool, set[str]]:
        """
        Validate that all required stems are present.

        Returns:
            Tuple of (is_valid, missing_stems)
        """
        missing = cls.REQUIRED_STEMS - set(stem_files.keys())
        return len(missing) == 0, missing
