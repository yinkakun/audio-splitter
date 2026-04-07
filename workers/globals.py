from services.audio_processor import DefaultSeparatorProvider

_separator_provider: DefaultSeparatorProvider | None = None


def set_global_separator_provider(provider: DefaultSeparatorProvider) -> None:
    global _separator_provider
    _separator_provider = provider


def get_global_separator_provider() -> DefaultSeparatorProvider | None:
    return _separator_provider
