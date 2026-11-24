"""Report only HTTP status and Google error status; never print credentials or response text."""
import json
import os
import urllib.error
import urllib.request

req = urllib.request.Request(
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "Content-Type": "application/json"},
    data=json.dumps({"model": "gemini-4-hydrogen", "messages": [{"role": "user", "content": "Reply OK"}]}).encode(),
)
try:
    with urllib.request.urlopen(req, timeout=30) as response:
        print("Gemini connectivity: HTTP", response.status)
except urllib.error.HTTPError as exc:
    try:
        status = json.load(exc).get("error", {}).get("status", "unknown")
        if not isinstance(status, str) or not status.replace("_", "").isalnum(): status = "unknown"
    except Exception:
        status = "unknown"
    print("Gemini connectivity: HTTP", exc.code, "status", status)
    raise SystemExit(1)
