"""Provide the minimal agent settings required at import time."""

import os

os.environ.setdefault("AGENT_TOKEN", "test-token")
os.environ.setdefault("SHORT_ID", "0123456789abcdef")
os.environ.setdefault("PUBLIC_KEY", "test-public-key")
os.environ.setdefault("HOST_IP", "203.0.113.10")
