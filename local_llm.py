"""
Local AI on the home server (Ollama on the SER9 Pro, or the Aero's GTX 1060). Free, private, always
available, and much weaker than Claude, so it does the high-volume first pass:
screening every headline, so Claude's share of your plan goes to what matters.
It never places trades.

  ollama pull llama3.1:8b        # default; the SER9 Pro (32 GB) also fits 14B models; set LOCAL_MODEL
"""
import json
import urllib.request

import config as C


class LocalUnavailable(Exception):
    pass


def available() -> bool:
    if not C.LOCAL_LLM_ENABLED:
        return False
    try:
        with urllib.request.urlopen(f"{C.LOCAL_LLM_URL}/api/tags", timeout=3):
            return True
    except Exception:
        return False


def chat_json(system: str, prompt: str, schema: dict, timeout: int = 180) -> dict:
    """One Ollama chat call constrained to `schema` (Ollama structured outputs)."""
    if not C.LOCAL_LLM_ENABLED:
        raise LocalUnavailable("local AI is turned off (LOCAL_LLM_ENABLED)")
    body = json.dumps({
        "model": C.LOCAL_MODEL, "stream": False, "format": schema,
        "options": {"temperature": 0},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(f"{C.LOCAL_LLM_URL}/api/chat", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except Exception as e:
        raise LocalUnavailable(f"Ollama not reachable at {C.LOCAL_LLM_URL}: {e}")
    return json.loads(data["message"]["content"])
