"""
Meta-tools backing progressive tool disclosure.

``workspace_tools`` lets a client widen or narrow the advertised surface at
runtime; ``describe_tool`` returns the full documentation for any registered
tool, including one that is currently hidden. Both are always advertised -- see
``core.tool_profiles.ALWAYS_VISIBLE`` -- because a client on a reduced profile
would otherwise have no route back to the rest of the server.
"""

import json
import logging
from typing import Any, Dict, Optional

from core.server import server
from core.tool_profiles import PROFILES, get_state

logger = logging.getLogger(__name__)

_ACTIONS = ("list_groups", "activate", "deactivate", "set_profile")


async def _all_tools() -> Dict[str, Any]:
    """Every registered tool, hidden ones included, keyed by name.

    Deliberately calls the *base* listing rather than the filtered override, so
    describe_tool can document a tool the current profile hides.
    """
    from fastmcp import FastMCP

    tools = await FastMCP.list_tools(server, run_middleware=False)
    return {tool.name: tool for tool in tools}


@server.tool(
    name="workspace_tools",
    description=(
        "Inspect and change which Google Workspace tools this server advertises. "
        "Hidden tools remain callable by name -- this only controls discovery. "
        "Actions: list_groups (show every service group, its tools and whether it "
        "is active), activate/deactivate (toggle one service group, e.g. 'gmail'), "
        "set_profile (core|extended|complete baseline surface)."
    ),
)
async def workspace_tools(
    action: str,
    group: Optional[str] = None,
    profile: Optional[str] = None,
) -> str:
    """Inspect or change the advertised tool surface.

    Args:
        action: One of list_groups, activate, deactivate, set_profile.
        group: Service group name, required for activate/deactivate.
        profile: One of core, extended, complete; required for set_profile.
    """
    state = get_state()
    action = (action or "").strip().lower()

    if action not in _ACTIONS:
        return json.dumps(
            {"error": f"unknown action {action!r}", "valid_actions": list(_ACTIONS)},
            indent=2,
        )

    if action in ("activate", "deactivate"):
        if not group:
            return json.dumps(
                {"error": f"{action} requires 'group'", "valid_groups": state.known_services()},
                indent=2,
            )
        ok = state.activate(group) if action == "activate" else state.deactivate(group)
        if not ok:
            return json.dumps(
                {"error": f"unknown group {group!r}", "valid_groups": state.known_services()},
                indent=2,
            )
        await _notify_list_changed()
        return json.dumps(
            {
                "action": action,
                "group": group.strip().lower(),
                "profile": state.profile,
                "active_groups": sorted(state.active_groups),
                "note": "tools/list has changed; re-fetch it",
            },
            indent=2,
        )

    if action == "set_profile":
        if not profile:
            return json.dumps(
                {"error": "set_profile requires 'profile'", "valid_profiles": list(PROFILES)},
                indent=2,
            )
        applied = state.set_profile(profile)
        await _notify_list_changed()
        return json.dumps(
            {
                "action": action,
                "profile": applied,
                "requested": profile,
                "active_groups": sorted(state.active_groups),
                "note": "tools/list has changed; re-fetch it",
            },
            indent=2,
        )

    # list_groups
    tools = await _all_tools()
    registered = set(tools)
    active = state.active_groups
    groups = []
    for service in state.known_services():
        names = sorted(n for n in registered if state.service_of(n) == service)
        if not names:
            continue  # service not loaded in this process
        visible = [n for n in names if state.is_visible(n)]
        groups.append(
            {
                "group": service,
                "active": service in active,
                "tools_total": len(names),
                "tools_visible": len(visible),
                "hidden": sorted(set(names) - set(visible)),
            }
        )
    return json.dumps(
        {
            "profile": state.profile,
            "valid_profiles": list(PROFILES),
            "active_groups": sorted(active),
            "groups": groups,
            "hint": (
                "Hidden tools are still callable by name. Use describe_tool for full "
                "documentation, or activate a group to advertise it."
            ),
        },
        indent=2,
    )


@server.tool(
    name="describe_tool",
    description=(
        "Return the full description and parameter schema for any tool on this "
        "server, including tools the current profile hides from tools/list. Call "
        "this before using an unfamiliar tool -- the live listing may carry only "
        "a one-line summary."
    ),
)
async def describe_tool(name: str) -> str:
    """Full documentation for one tool, listed or not.

    Args:
        name: Exact tool name, e.g. 'draft_gmail_message'.
    """
    tools = await _all_tools()
    tool = tools.get(name)
    if tool is None:
        state = get_state()
        suggestions = sorted(n for n in tools if name and name.lower() in n.lower())[:10]
        return json.dumps(
            {
                "error": f"no tool named {name!r}",
                "suggestions": suggestions,
                "profile": state.profile,
            },
            indent=2,
        )
    state = get_state()
    return json.dumps(
        {
            "name": tool.name,
            "service": state.service_of(tool.name),
            "visible_in_tools_list": state.is_visible(tool.name),
            "callable": True,
            "description": tool.description,
            "parameters": tool.parameters,
        },
        indent=2,
        default=str,
    )


async def _notify_list_changed() -> None:
    """Tell the client its cached tools/list is stale.

    Best-effort: the notification is an optimisation, and a client that re-lists
    on its own still sees the new surface. Never let a transport that cannot
    deliver it fail the meta-tool call.
    """
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
        await ctx.send_tool_list_changed()
    except Exception as exc:  # noqa: BLE001 - advisory only
        logger.debug("tools/list_changed notification not delivered: %s", exc)
