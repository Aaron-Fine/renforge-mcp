"""Mod authoring and live-test MCP tools."""

from __future__ import annotations

TOOL_NAMES = ("renforge_mod",)


def build_wrappers(context):
    from ..tools import mods as mod_tools

    _log_tool_call = context.log_tool_call

    def renforge_mod(
        project_path: str,
        action: str,
        name: str | None = None,
        path: str | None = None,
        content: str | None = None,
    ) -> dict:
        """Write, test, or remove a mod under `game/mods/<name>/`."""
        return _log_tool_call(
            name="renforge_mod",
            params={
                "project_path": project_path,
                "action": action,
                "name": name,
                "path": path,
                "content_bytes": len(content.encode("utf-8")) if isinstance(content, str) else None,
            },
            project_root=project_path,
            fn=mod_tools.mod,
            args=(project_path, action),
            kwargs={"name": name, "path": path, "content": content},
        )

    return {name: value for name, value in locals().items() if name in TOOL_NAMES}


def register(registrar, wrappers) -> None:
    registrar.register_many(wrappers, TOOL_NAMES)


__all__ = ["TOOL_NAMES", "build_wrappers", "register"]
