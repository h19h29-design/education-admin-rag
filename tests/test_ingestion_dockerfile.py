from pathlib import Path


def test_ingestion_image_enforces_linux_amd64_platform() -> None:
    dockerfile = Path("docker/ingestion.Dockerfile")
    from_instruction = dockerfile.read_text().splitlines()[0]

    assert from_instruction.startswith("FROM --platform=linux/amd64 ")
