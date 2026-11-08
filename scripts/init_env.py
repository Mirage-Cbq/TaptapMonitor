"""Create a private .env without printing its token or overwriting an existing file."""
from pathlib import Path
import secrets

destination = Path(__file__).resolve().parent.parent / ".env"
if destination.exists():
    print(".env already exists; kept unchanged.")
else:
    with destination.open("x", encoding="utf-8") as output:
        output.write(f"MONITOR_API_TOKEN={secrets.token_urlsafe(32)}\nMONITOR_PORT=8080\nASTRBOT_API_KEY=\nASTRBOT_BASE_URL=\n")
    destination.chmod(0o600)
    print("Created .env with a random management token. Open the file to retrieve it.")
