import re
import tomllib
from pathlib import Path


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
        for dependency in root_package.get("optional-dependencies", {}).get(
            "index", []
        )
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
    runtime_stage = Path("docker/ingestion.Dockerfile").read_text().split(
        " AS runtime\n", maxsplit=1
    )[1]
    assert "prepare_ocr_models" not in runtime_stage
    assert "COPY docker" not in runtime_stage
    runtime_python = Path("src/ingestion/extract_ocr.py").read_text() + Path(
        "src/cli.py"
    ).read_text()
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
        "PADDLE_PDX_CACHE_HOME=/tmp/paddlex-cache" in line
        for line in instructions
    )
    assert any(
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=1" in line
        for line in instructions
    )
    assert any(
        "SEN_QA_OCR_MODEL_ROOT=/opt/models/paddleocr" in line
        for line in instructions
    )
    assert not any(
        "PADDLE_HOME=/opt/models" in line
        or "PADDLE_PDX_CACHE_HOME=/opt/models" in line
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
    assert "snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" in dockerfile
    assert "Acquire::Check-Valid-Until=false" in dockerfile
    assert 'VERSION_ID" = "13"' in dockerfile
    assert 'VERSION_CODENAME" = "trixie"' in dockerfile
    assert (
        'grep -Fqx "# http://snapshot.debian.org/archive/debian/' in dockerfile
    )
    assert (
        'grep -Fqx "# http://snapshot.debian.org/archive/debian-security/'
        in dockerfile
    )
    assert 'grep -Fqx "URIs: https://snapshot.debian.org/archive/debian/' in dockerfile
    assert (
        'grep -Fqx "URIs: https://snapshot.debian.org/archive/debian-security/'
        in dockerfile
    )
    assert '! grep -Fq "URIs: http://deb.debian.org/"' in dockerfile
    for package in ("libgomp1", "libgl1", "libglib2.0-0t64"):
        assert package in dockerfile
