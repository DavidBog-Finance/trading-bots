import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest


@pytest.fixture(autouse=True)
def _offline_settings(monkeypatch):
    """Tests must not depend on the real environment (GitHub passes the live secrets and variables
    to every step): no phone pushes, no webhook calls, and no deposit override leaking in."""
    import config as C
    for name, value in {"NTFY_TOPIC": "", "ALERT_WEBHOOK_URL": "", "CONTROL_TOPIC": "",
                        "HEALTHCHECK_URL": "", "NET_DEPOSITS_OVERRIDE": 0}.items():
        if hasattr(C, name):
            monkeypatch.setattr(C, name, value)
    yield
    import broker as B
    B._trade_lines.clear()  # or the exit hook would push test trades to the real phone topic
