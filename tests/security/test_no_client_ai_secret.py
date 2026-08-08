from pathlib import Path


def test_legacy_html_has_no_client_side_model_secret_or_direct_endpoint() -> None:
    """Removing the browser model integration must not reintroduce a credential or API sink."""
    html = Path("교육행정_AI_Launcher.html").read_text(encoding="utf-8")

    has_google_model_endpoint = "generativelanguage.googleapis.com" in html
    has_google_api_key_marker = "AIza" in html

    assert not has_google_model_endpoint, "browser Google model endpoint remains"
    assert not has_google_api_key_marker, "browser API-key-shaped marker remains"
