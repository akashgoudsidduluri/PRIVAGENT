"""Provider package for PrivAgent."""
from typing import Any

def get_provider() -> Any:
    from app.agentLoop import _provider_for_config
    return _provider_for_config()

