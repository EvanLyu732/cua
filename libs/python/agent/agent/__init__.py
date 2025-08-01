"""
agent - Decorator-based Computer Use Agent with liteLLM integration
"""

import logging
import sys

from .decorators import agent_loop
from .agent import ComputerAgent
from .types import Messages, AgentResponse

# Import loops to register them
from . import loops

__all__ = [
    "agent_loop",
    "ComputerAgent",
    "Messages",
    "AgentResponse"
]

__version__ = "0.4.0"

logger = logging.getLogger(__name__)

# Initialize telemetry when the package is imported
try:
    # Import from core telemetry for basic functions
    from core.telemetry import (
        is_telemetry_enabled,
        flush,
        record_event,
    )

    # Import set_dimension from our own telemetry module
    from .telemetry import set_dimension

    # Check if telemetry is enabled
    if is_telemetry_enabled():
        logger.info("Telemetry is enabled")

        # Record package initialization
        record_event(
            "module_init",
            {
                "module": "agent",
                "version": __version__,
                "python_version": sys.version,
            },
        )

        # Set the package version as a dimension
        set_dimension("agent_version", __version__)

        # Flush events to ensure they're sent
        flush()
    else:
        logger.info("Telemetry is disabled")
except ImportError as e:
    # Telemetry not available
    logger.warning(f"Telemetry not available: {e}")
except Exception as e:
    # Other issues with telemetry
    logger.warning(f"Error initializing telemetry: {e}")
