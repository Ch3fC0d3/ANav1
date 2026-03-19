from __future__ import annotations

import re
import shutil
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT_DIR / "app" / "templates" / "index.html"
STATIC_DIR = ROOT_DIR / "app" / "static"
MOBILE_DIR = ROOT_DIR / "mobile"
MOBILE_STATIC_DIR = MOBILE_DIR / "static"
RUNTIME_CONFIG_PATH = MOBILE_DIR / "runtime-config.js"

DEFAULT_RUNTIME_CONFIG = """window.ANAV1_CONFIG = window.ANAV1_CONFIG || {
  // Leave blank for local /mobile-preview testing.
  // For Android/iPhone builds, set this to your Railway HTTPS URL with no trailing slash.
  apiBaseUrl: "",
};
"""


def build_mobile_html(template_text: str) -> str:
    html = template_text
    html = html.replace("{{ app_name }}", "ANav1 Mobile")
    html = html.replace(
        "{{ 'status-on' if openai_configured else 'status-off' }}",
        "status-off",
    )
    html = html.replace(
        "{{ 'Configured' if openai_configured else 'Not configured' }}",
        "Server status loading...",
    )
    html = re.sub(
        r"{% if openai_configured %}.*?{% endif %}",
        (
            "Connect this mobile shell to your ANav1 server. "
            "When the app loads, it will fetch the real server status automatically."
        ),
        html,
        flags=re.S,
    )
    html = html.replace("{{ max_upload_mb }} MB", "Server limit")
    html = html.replace('href="/static/favicon.svg"', 'href="./static/favicon.svg"')
    html = html.replace('href="/static/styles.css"', 'href="./static/styles.css"')
    html = html.replace(
        '<script src="/static/app.js" defer></script>',
        '<script src="./runtime-config.js"></script>\n    <script src="./static/app.js" defer></script>',
    )
    return html


def main() -> None:
    MOBILE_DIR.mkdir(parents=True, exist_ok=True)
    MOBILE_STATIC_DIR.mkdir(parents=True, exist_ok=True)

    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")
    mobile_html = build_mobile_html(template_text)
    (MOBILE_DIR / "index.html").write_text(mobile_html, encoding="utf-8")

    shutil.copytree(STATIC_DIR, MOBILE_STATIC_DIR, dirs_exist_ok=True)

    if not RUNTIME_CONFIG_PATH.exists():
        RUNTIME_CONFIG_PATH.write_text(DEFAULT_RUNTIME_CONFIG, encoding="utf-8")


if __name__ == "__main__":
    main()
