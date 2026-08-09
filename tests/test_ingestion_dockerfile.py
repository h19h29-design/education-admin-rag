import re
import tomllib
from pathlib import Path

import pytest

_DOCKER_CONTEXT_EXCEPTIONS = (
    "pyproject.toml",
    "uv.lock",
    "src/",
    "src/*.py",
    "src/corpus/",
    "src/corpus/*.py",
    "src/evaluation/",
    "src/evaluation/*.py",
    "src/ingestion/",
    "src/ingestion/*.py",
    "src/retrieval/",
    "src/retrieval/*.py",
    "config/models.lock.json",
    "data/manifests/sen_qa_sources.json",
    "docker/ingestion.Dockerfile",
    "docker/prepare_ocr_models.py",
    "docker/indexer.Dockerfile",
    "docker/prepare_embedding_model.py",
    "config/backup-tools.lock.json",
    "docker/backup.Dockerfile",
    "docker/prepare_backup_tools.py",
)


def _docker_context_rules() -> tuple[str, ...]:
    dockerignore = Path(".dockerignore")
    assert dockerignore.is_file(), "the ingestion build context must be deny-by-default"
    return tuple(
        line
        for raw_line in dockerignore.read_text(encoding="utf-8").splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    )


def _docker_pattern_regex(pattern: str) -> re.Pattern[str]:
    """Compile the Docker ``**`` and path-segment glob rules used here."""
    pieces = ["^"]
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*" and pattern[index : index + 2] == "**":
            index += 2
            while index < len(pattern) and pattern[index] == "*":
                index += 1
            if index < len(pattern) and pattern[index] == "/":
                pieces.append("(?:.*/)?")
                index += 1
            else:
                pieces.append(".*")
            continue
        if character == "*":
            pieces.append("[^/]*")
        elif character == "?":
            pieces.append("[^/]")
        elif character == "[":
            closing = pattern.find("]", index + 1)
            if closing == -1:
                pieces.append(r"\[")
            else:
                character_class = pattern[index + 1 : closing]
                if character_class.startswith("!"):
                    character_class = "^" + character_class[1:]
                pieces.append("[" + character_class + "]")
                index = closing
        else:
            pieces.append(re.escape(character))
        index += 1
    pieces.append("$")
    return re.compile("".join(pieces))


def _docker_pattern_matches(pattern: str, path: str) -> bool:
    """Match a path or any parent, as Moby's MatchesOrParentMatches does."""
    normalized_pattern = pattern.strip("/")
    normalized_path = path.strip("/")
    parts = normalized_path.split("/")
    candidates = (normalized_path,) + tuple(
        "/".join(parts[:end]) for end in range(1, len(parts))
    )
    expression = _docker_pattern_regex(normalized_pattern)
    return any(expression.fullmatch(candidate) is not None for candidate in candidates)


def _docker_rules_include(rules: tuple[str, ...], path: str) -> bool:
    """Evaluate ordered Docker ignore rules; ``True`` means sent in context."""
    ignored = False
    for raw_rule in rules:
        rule = raw_rule.strip()
        if not rule or rule.startswith("#"):
            continue
        negated = rule.startswith("!")
        pattern = rule[1:] if negated else rule
        if negated != ignored:
            continue
        if _docker_pattern_matches(pattern, path):
            ignored = not negated
    return not ignored


def _docker_can_descend(rules: tuple[str, ...], directory: str) -> bool:
    """Model Moby's exception-prefix check before pruning an ignored directory."""
    if _docker_rules_include(rules, directory):
        return True
    directory_prefix = directory.strip("/") + "/"
    return any(
        (rule[1:].strip("/") + "/").startswith(directory_prefix)
        for rule in rules
        if rule.startswith("!")
    )


def _docker_context_contains(rules: tuple[str, ...], path: str) -> bool:
    parts = path.strip("/").split("/")
    parents = tuple("/".join(parts[:end]) for end in range(1, len(parts)))
    return all(_docker_can_descend(rules, parent) for parent in parents) and (
        _docker_rules_include(rules, path)
    )


def _is_included_in_docker_context(path: str) -> bool:
    return _docker_context_contains(_docker_context_rules(), path)


def test_docker_context_oracle_models_moby_parent_and_ordered_semantics() -> None:
    """Catches a Git-style oracle hiding Docker parent-directory reinclusion."""
    assert _docker_rules_include(("**", "!src/"), "src/private.json")
    assert not _docker_rules_include(
        ("**", "!src/", "src/private.json"), "src/private.json"
    )
    python_only = ("**", "!src/**/*.py")
    assert _docker_rules_include(python_only, "src/cli.py")
    assert _docker_rules_include(python_only, "src/nested/module.py")
    assert not _docker_rules_include(python_only, "src/nested/private.json")
    assert _docker_context_contains(python_only, "src/cli.py")
    assert not _docker_context_contains(python_only, "src/nested/module.py")


def test_ingestion_image_enforces_linux_amd64_platform() -> None:
    dockerfile = Path("docker/ingestion.Dockerfile")
    from_instruction = dockerfile.read_text().splitlines()[0]

    assert from_instruction.startswith("FROM --platform=linux/amd64 ")


def test_ingestion_dependency_boundary_excludes_index_stack_from_base() -> None:
    """Catches vector/embedding dependencies leaking into the OCR image graph."""
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    root_package = next(
        package
        for package in lock["package"]
        if package["name"] == "education-admin-rag"
    )
    docker_sync = next(
        line
        for line in Path("docker/ingestion.Dockerfile").read_text().splitlines()
        if line.startswith("RUN uv sync")
    )

    assert project["project"]["dependencies"] == ["pydantic", "pymupdf", "typer"]
    assert project["project"]["optional-dependencies"].get("index", []) == [
        "qdrant-client",
        "sentence-transformers",
    ]
    assert project["project"]["optional-dependencies"]["ocr"] == [
        "paddleocr; sys_platform == 'linux' and platform_machine == 'x86_64'",
        "paddlepaddle==3.1.1; sys_platform == 'linux' and platform_machine == 'x86_64'",
    ]
    assert [dependency["name"] for dependency in root_package["dependencies"]] == [
        "pydantic",
        "pymupdf",
        "typer",
    ]
    assert [
        dependency["name"]
        for dependency in root_package.get("optional-dependencies", {}).get("index", [])
    ] == ["qdrant-client", "sentence-transformers"]
    assert docker_sync == "RUN uv sync --frozen --extra ocr --no-dev"


def test_ingestion_image_is_digest_pinned_multistage_with_frozen_venv_and_models() -> (
    None
):
    """Catches an unpinned/single-stage image or runtime dependency/model download."""
    instructions = Path("docker/ingestion.Dockerfile").read_text().splitlines()
    from_instructions = [line for line in instructions if line.startswith("FROM ")]

    assert len(from_instructions) >= 2
    assert all(
        line.startswith("FROM --platform=linux/amd64 ") and "@sha256:" in line
        for line in from_instructions
    )
    assert any("uv sync --frozen --extra ocr --no-dev" in line for line in instructions)
    assert any(
        "COPY docker/prepare_ocr_models.py /build/prepare_ocr_models.py" in line
        for line in instructions
    )
    assert any("/build/prepare_ocr_models.py" in line for line in instructions)
    assert not any("src.cli prepare-ocr-models" in line for line in instructions)
    assert any("validate-ocr-models" in line for line in instructions)
    assert any(
        "COPY --from=builder /opt/venv /opt/venv" in line for line in instructions
    )
    assert any(
        "COPY --from=builder /opt/models/paddleocr /opt/models/paddleocr" in line
        for line in instructions
    )
    runtime_stage = (
        Path("docker/ingestion.Dockerfile")
        .read_text()
        .split(" AS runtime\n", maxsplit=1)[1]
    )
    assert "prepare_ocr_models" not in runtime_stage
    assert "COPY docker" not in runtime_stage
    runtime_python = (
        Path("src/ingestion/extract_ocr.py").read_text()
        + Path("src/cli.py").read_text()
    )
    assert "urlopen" not in runtime_python
    assert "download_locked_archive" not in runtime_python
    assert "prepare_model_staging" not in runtime_python


def test_ingestion_runtime_is_nonroot_and_uses_locked_local_model_paths() -> None:
    """Catches a root runtime, uv re-resolution, or a writable model directory."""
    instructions = Path("docker/ingestion.Dockerfile").read_text().splitlines()

    assert any(
        line.startswith("USER ") and line != "USER root" for line in instructions
    )
    assert any("PYTHONPATH=/work" in line for line in instructions)
    assert any("PADDLE_HOME=/tmp/paddle-home" in line for line in instructions)
    assert any(
        "PADDLE_PDX_CACHE_HOME=/tmp/paddlex-cache" in line for line in instructions
    )
    assert any(
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=1" in line for line in instructions
    )
    assert any(
        "SEN_QA_OCR_MODEL_ROOT=/opt/models/paddleocr" in line for line in instructions
    )
    assert not any(
        "PADDLE_HOME=/opt/models" in line or "PADDLE_PDX_CACHE_HOME=/opt/models" in line
        for line in instructions
    )
    assert instructions[-1] == 'CMD ["/opt/venv/bin/python", "-m", "src.cli"]'


def test_ingestion_runtime_installs_trixie_elf_dependencies_from_frozen_snapshot() -> (
    None
):
    """Catches missing Paddle/OpenCV shared libraries or mutable apt inputs."""
    dockerfile = Path("docker/ingestion.Dockerfile").read_text()

    assert re.search(r"DEBIAN_SNAPSHOT=\d{8}T\d{6}Z", dockerfile)
    assert "snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" in dockerfile
    assert (
        "snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" in dockerfile
    )
    assert "Acquire::Check-Valid-Until=false" in dockerfile
    assert 'VERSION_ID" = "13"' in dockerfile
    assert 'VERSION_CODENAME" = "trixie"' in dockerfile
    assert 'grep -Fqx "# http://snapshot.debian.org/archive/debian/' in dockerfile
    assert (
        'grep -Fqx "# http://snapshot.debian.org/archive/debian-security/' in dockerfile
    )
    assert 'grep -Fqx "URIs: https://snapshot.debian.org/archive/debian/' in dockerfile
    assert (
        'grep -Fqx "URIs: https://snapshot.debian.org/archive/debian-security/'
        in dockerfile
    )
    assert '! grep -Fq "URIs: http://deb.debian.org/"' in dockerfile
    for package in ("libgomp1", "libgl1", "libglib2.0-0t64"):
        assert package in dockerfile


def test_ingestion_docker_context_includes_every_local_build_input() -> None:
    """Catches the deny-by-default context omitting a Docker COPY source."""
    dockerfile = Path("docker/ingestion.Dockerfile")
    local_copy_sources = tuple(
        source
        for line in dockerfile.read_text(encoding="utf-8").splitlines()
        if line.startswith("COPY ") and "--from=" not in line
        for source in line.split()[1:-1]
    )

    assert local_copy_sources == (
        "pyproject.toml",
        "uv.lock",
        "src",
        "config/models.lock.json",
        "data/manifests/sen_qa_sources.json",
        "docker/prepare_ocr_models.py",
    )
    required_paths = (
        "pyproject.toml",
        "uv.lock",
        "config/models.lock.json",
        "data/manifests/sen_qa_sources.json",
        "docker/ingestion.Dockerfile",
        "docker/prepare_ocr_models.py",
        *(path.as_posix() for path in sorted(Path("src").rglob("*.py"))),
    )
    excluded_required_paths = tuple(
        path for path in required_paths if not _is_included_in_docker_context(path)
    )

    assert excluded_required_paths == ()


@pytest.mark.parametrize(
    "candidate",
    (
        "artifacts/run.txt",
        "src/artifacts",
        "src/artifacts/run.txt",
        ".git/config",
        "src/.git",
        "src/.git/config",
        ".env",
        ".env.production",
        "src/.env",
        "source.pdf",
        "source.PDF",
        "src/source.pdf",
        "output.jsonl",
        "src/output.JSONL",
        "review.sqlite",
        "src/review.sqlite3",
        "private.key",
        "src/private.pem",
        "src/private.json",
        "src/private.JSON",
        "src/ingestion/private.yaml",
        "src/ingestion/private.YAML",
        "src/private.yml",
        "src/ingestion/private.toml",
        "src/ingestion/private.TOML",
        "src/private.txt",
        "src/private.TXT",
        "src/ingestion/private.bin",
        "src/ingestion/private.BIN",
        "src/private.PY",
        "src/.ENV",
        "src/.Env.production",
        "src/__pycache__/private.py",
        "src/__PYCACHE__/private.py",
        "src/private.PYC",
        "src/private.PYO",
        "src/future/module.py",
        "src/corpus/nested/module.py",
        "src/ingestion/package.py/private.json",
        "config/arbitrary.json",
        "config/private/settings.toml",
        "data/arbitrary.json",
        "data/manifests/unapproved.json",
        "data/raw/source.pdf",
        "docker/arbitrary.py",
        "docs/design.md",
        "src/docs",
        "src/docs/design.md",
        "tests/test_private.py",
        "src/tests",
        "src/tests/test_private.py",
        "future/arbitrary.txt",
    ),
)
def test_ingestion_docker_context_excludes_non_build_inputs(candidate: str) -> None:
    """Catches private, generated, or unrelated files entering the build context."""
    assert not _is_included_in_docker_context(candidate)


def test_ingestion_docker_context_has_no_broad_reinclude() -> None:
    """Catches a later exception widening the reviewed context allowlist."""
    exceptions = tuple(
        rule.removeprefix("!")
        for rule in _docker_context_rules()
        if rule.startswith("!")
    )

    assert exceptions == _DOCKER_CONTEXT_EXCEPTIONS
