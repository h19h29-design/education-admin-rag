import json
import re
from pathlib import Path


def test_legacy_html_has_no_client_side_model_secret_or_direct_endpoint() -> None:
    """Removing the browser model integration must not reintroduce a credential or API sink."""
    html = Path("교육행정_AI_Launcher.html").read_text(encoding="utf-8")

    has_google_model_endpoint = "generativelanguage.googleapis.com" in html
    has_google_api_key_marker = "AIza" in html
    has_removed_model_helper = "geminiAsk" in html
    has_removed_answer_helper = "geminiAnswer" in html
    has_direct_network_sink = "fetch(" in html
    has_generic_knowledge_fallback = "일반 지식으로" in html

    assert not has_google_model_endpoint, "browser Google model endpoint remains"
    assert not has_google_api_key_marker, "browser API-key-shaped marker remains"
    assert not has_removed_model_helper, "browser model helper remains"
    assert not has_removed_answer_helper, "browser answer helper remains"
    assert not has_direct_network_sink, "browser direct network sink remains"
    assert not has_generic_knowledge_fallback, "browser generic-knowledge fallback remains"


def test_legacy_html_keeps_embedded_case_data_for_local_search() -> None:
    html = Path("교육행정_AI_Launcher.html").read_text(encoding="utf-8")
    app_match = re.search(r"window\.APP = (.+);\nconst A = window\.APP;", html, re.DOTALL)

    assert app_match is not None, "embedded local data payload is missing"
    app = json.loads(app_match.group(1))
    case_ids = {case["id"] for case in app["cases"]}

    assert "AC-2020-001" in case_ids
