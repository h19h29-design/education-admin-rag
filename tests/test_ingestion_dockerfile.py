from pathlib import Path


def test_ingestion_image_enforces_linux_amd64_platform() -> None:
    dockerfile = Path("docker/ingestion.Dockerfile")
    from_instruction = dockerfile.read_text().splitlines()[0]

    assert from_instruction.startswith("FROM --platform=linux/amd64 ")


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
    assert any("prepare-ocr-models" in line for line in instructions)
    assert any("validate-ocr-models" in line for line in instructions)
    assert any(
        "COPY --from=builder /opt/venv /opt/venv" in line for line in instructions
    )
    assert any(
        "COPY --from=builder /opt/models/paddleocr /opt/models/paddleocr" in line
        for line in instructions
    )


def test_ingestion_runtime_is_nonroot_and_uses_locked_local_model_paths() -> None:
    """Catches a root runtime, uv re-resolution, or a writable network-backed model cache."""
    instructions = Path("docker/ingestion.Dockerfile").read_text().splitlines()

    assert any(
        line.startswith("USER ") and line != "USER root" for line in instructions
    )
    assert any("PYTHONPATH=/work" in line for line in instructions)
    assert any("PADDLE_HOME=/opt/models/paddleocr" in line for line in instructions)
    assert instructions[-1] == 'CMD ["/opt/venv/bin/python", "-m", "src.cli"]'
