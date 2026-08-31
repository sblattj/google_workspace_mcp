"""
Progressive tool disclosure for workspace-mcp.

This server registers up to 122 tools. Advertising all of them on every
``tools/list`` costs a large, fixed slice of the model's context before it has
done any work, even though a given session usually touches one or two services.

This module implements the pattern cdp-toolkit 2.0.0 uses: advertise a small
baseline surface, keep the rest one meta-tool call away, and -- critically --
never actually disable anything. Hiding happens in ``list_tools`` only, and
FastMCP dispatches ``call_tool`` straight to its tool manager without consulting
``list_tools``, so a hidden tool invoked by name still executes normally.

Two axes:

* **profile** -- the baseline tier drawn from ``core/tool_tiers.yaml``:
  ``core`` (45 tools), ``extended`` (91) or ``complete`` (122, the default,
  which preserves the historical behaviour exactly).
* **groups** -- one per service (``gmail``, ``drive``, ...). Activating a group
  reveals every tool of that service regardless of the profile tier, so an agent
  that needs Gmail's full surface pays for Gmail alone rather than for all 12.

Configuration is via ``WORKSPACE_MCP_TOOL_PROFILE``. The default is ``complete``
so that upgrading changes nothing until an operator opts in.
"""

import logging
import os
import threading
from typing import Dict, List, Optional, Set

from core.tool_tier_loader import ToolTierLoader

logger = logging.getLogger(__name__)

PROFILES = ("core", "extended", "complete")
DEFAULT_PROFILE = "complete"

ENV_PROFILE = "WORKSPACE_MCP_TOOL_PROFILE"

# Meta-tools are always advertised; without them a client on a reduced profile
# would have no way to discover or reach anything that is hidden.
ALWAYS_VISIBLE = frozenset({"workspace_tools", "describe_tool"})


def _normalize_profile(value: Optional[str]) -> str:
    """Coerce a profile name to a supported value, warning on anything else."""
    if not value:
        return DEFAULT_PROFILE
    candidate = value.strip().lower()
    if candidate in PROFILES:
        return candidate
    logger.warning(
        "Unknown %s=%r; falling back to %r (valid: %s)",
        ENV_PROFILE,
        value,
        DEFAULT_PROFILE,
        ", ".join(PROFILES),
    )
    return DEFAULT_PROFILE


class ToolProfileState:
    """Mutable, process-wide visibility state for tools/list.

    Guarded by a lock because ``workspace_tools`` mutates it from request
    handlers while ``list_tools`` reads it.
    """

    def __init__(self, loader: Optional[ToolTierLoader] = None):
        self._lock = threading.RLock()
        self._loader = loader or ToolTierLoader()
        self._profile = _normalize_profile(os.getenv(ENV_PROFILE))
        self._active_groups: Set[str] = set()
        self._tool_to_service: Optional[Dict[str, str]] = None
        self._service_tools: Optional[Dict[str, Dict[str, List[str]]]] = None

    # -- lazily-built indexes -------------------------------------------------

    def _config(self) -> Dict[str, Dict[str, List[str]]]:
        if self._service_tools is None:
            self._service_tools = self._loader._load_config()
        return self._service_tools

    def _index(self) -> Dict[str, str]:
        """Map every known tool name to the service that owns it."""
        if self._tool_to_service is None:
            index: Dict[str, str] = {}
            for service, tiers in self._config().items():
                for names in tiers.values():
                    for name in names or []:
                        index[name] = service
            self._tool_to_service = index
        return self._tool_to_service

    def _tier_tools(self, profile: str) -> Set[str]:
        """Every tool included at or below the given tier, across all services.

        Note ``get_tools_for_tier`` returns that tier ALONE; the cumulative
        accessor is ``get_tools_up_to_tier``. Using the former here would hide
        every core tool whenever the profile was ``extended``.
        """
        return set(self._loader.get_tools_up_to_tier(profile))

    # -- queries --------------------------------------------------------------

    @property
    def profile(self) -> str:
        with self._lock:
            return self._profile

    @property
    def active_groups(self) -> Set[str]:
        with self._lock:
            return set(self._active_groups)

    @property
    def is_reduced(self) -> bool:
        """True when the advertised surface is smaller than everything."""
        with self._lock:
            return self._profile != "complete"

    def known_services(self) -> List[str]:
        return sorted(self._config().keys())

    def service_of(self, tool_name: str) -> Optional[str]:
        return self._index().get(tool_name)

    def is_visible(self, tool_name: str) -> bool:
        """Whether ``tools/list`` should advertise this tool right now.

        Invisible never means uncallable -- see the module docstring.
        """
        if tool_name in ALWAYS_VISIBLE:
            return True
        with self._lock:
            profile = self._profile
            active = set(self._active_groups)
        if profile == "complete":
            return True
        service = self.service_of(tool_name)
        if service is not None and service in active:
            return True
        # A tool absent from tool_tiers.yaml has no tier to be judged by. Show
        # it rather than silently hiding something this module does not model.
        if service is None:
            return True
        return tool_name in self._tier_tools(profile)

    # -- mutations ------------------------------------------------------------

    def set_profile(self, profile: str) -> str:
        normalized = _normalize_profile(profile)
        with self._lock:
            self._profile = normalized
        logger.info("Tool profile set to %r", normalized)
        return normalized

    def activate(self, group: str) -> bool:
        """Reveal a service's full tool surface. False if the group is unknown."""
        group = (group or "").strip().lower()
        if group not in self._config():
            return False
        with self._lock:
            self._active_groups.add(group)
        return True

    def deactivate(self, group: str) -> bool:
        group = (group or "").strip().lower()
        if group not in self._config():
            return False
        with self._lock:
            self._active_groups.discard(group)
        return True


_state: Optional[ToolProfileState] = None


def get_state() -> ToolProfileState:
    """Process-wide singleton, created on first use."""
    global _state
    if _state is None:
        _state = ToolProfileState()
    return _state


def terse(description: Optional[str]) -> Optional[str]:
    """Reduce a full tool description to a one-line summary.

    The full text stays reachable through ``describe_tool``. Descriptions in
    this codebase are docstrings whose first paragraph is already a summary, so
    the first non-empty line is a good terse form.
    """
    if not description:
        return description
    for raw_line in description.splitlines():
        line = raw_line.strip()
        if line:
            return line
    return description
