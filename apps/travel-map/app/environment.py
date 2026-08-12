"""Safe, explicit local environment-file loading for operator scripts."""

from pathlib import Path

from dotenv import load_dotenv


class EnvironmentFileError(ValueError):
    """An explicitly requested local environment file cannot be trusted."""


def load_environment_file(path: Path | None) -> None:
    """Load an operator-selected dotenv file without overriding process secrets."""

    if path is None:
        return
    if path.is_symlink():
        raise EnvironmentFileError("environment file is invalid")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise EnvironmentFileError("environment file is unavailable") from exc
    if not resolved.is_file():
        raise EnvironmentFileError("environment file is invalid")
    load_dotenv(resolved, override=False, verbose=False)
