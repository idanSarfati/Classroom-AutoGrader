"""Build a Streamlit Community Cloud secrets.toml from the local secret files.

Writes the ready-to-paste TOML to ``streamlit_secrets.toml`` and prints only
non-sensitive metadata, so no credential is echoed into a terminal or a chat
transcript. The output file is git-ignored.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "streamlit_secrets.toml"

ENV = ROOT / ".env"
CREDENTIALS = ROOT / "credentials.json"
TOKEN = ROOT / "token.json"

# Scopes src/auth.py requires; a token missing any of them cannot be used.
REQUIRED_SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.students",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/drive",
]


def toml_basic(value: str) -> str:
    """Escape a Python string as a TOML basic (double-quoted) string."""
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{out}"'


def toml_literal_block(text: str, label: str) -> str:
    """Render ``text`` as a TOML multi-line literal string (''' block).

    Literal strings do not interpret backslash escapes, which is exactly what
    JSON needs - a token containing ``\\n`` must stay a literal backslash-n so
    ``json.loads`` sees it as an escape, not as a newline.
    """
    if "'''" in text:
        raise ValueError(f"{label} contains a ''' sequence and cannot be a TOML literal block")
    return "'''\n" + text.strip() + "\n'''"


def read_env_value(path: Path, key: str) -> str:
    """Pull a single key out of a .env file, honouring quotes and 'export'."""
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=\s*(.*?)\s*$")
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = pattern.match(line)
        if not match:
            continue
        raw = match.group(1)
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        if raw:
            return raw
    raise KeyError(f"{key} not found or empty in {path.name}")


def main() -> int:
    api_key = read_env_value(ENV, "GROQ_API_KEY")
    credentials = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
    token = json.loads(TOKEN.read_text(encoding="utf-8"))

    creds_text = json.dumps(credentials, indent=2, ensure_ascii=False)
    token_text = json.dumps(token, indent=2, ensure_ascii=False)

    # Re-parse what we are about to write, so a malformed block is caught here
    # rather than after it has been pasted into the Streamlit dashboard.
    json.loads(creds_text)
    json.loads(token_text)

    parts = [
        "# Streamlit Community Cloud secrets.",
        "# Paste this whole file into Settings -> Secrets on your Streamlit app.",
        "# Generated locally; do not commit (already git-ignored).",
        "",
        f"GROQ_API_KEY = {toml_basic(api_key)}",
        "",
        f"GOOGLE_CREDENTIALS = {toml_literal_block(creds_text, 'credentials.json')}",
        "",
        f"GOOGLE_TOKEN = {toml_literal_block(token_text, 'token.json')}",
        "",
    ]
    OUT.write_text("\n".join(parts), encoding="utf-8")

    client_type = "web" if "web" in credentials else "installed (desktop)"
    granted = set(token.get("scopes") or [])
    missing = [s for s in REQUIRED_SCOPES if s not in granted]

    print(f"Wrote {OUT.name} ({OUT.stat().st_size} bytes)")
    print(f"  GROQ_API_KEY         : present ({len(api_key)} chars, value not shown)")
    print(f"  GOOGLE_CREDENTIALS   : valid JSON, client type = {client_type}")
    print(f"  GOOGLE_TOKEN         : valid JSON, refresh_token = "
          f"{'present' if token.get('refresh_token') else 'MISSING'}")
    if missing:
        print(f"  WARNING: token is missing scope(s): {missing}")
    else:
        print("  Scopes               : all required scopes granted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
