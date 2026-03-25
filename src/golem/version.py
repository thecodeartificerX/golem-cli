"""Version information utilities for Golem."""

import platform
import sys

from golem import __version__


def get_version_info() -> dict[str, str]:
    """Return a dict with version, python, and platform strings."""
    return {
        "version": __version__,
        "python": platform.python_version(),
        "platform": sys.platform,
    }
