"""MCP application bootstrap and compatibility fallback."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass
class _FallbackServer:
    project_root: Path | None = None

    def run(self, *_, **__) -> int:
        print(
            "RenForge fallback mode: FastMCP backend unavailable. "
            "Install 'fastmcp' or 'mcp>=1.0.0' to enable MCP transport."
        )
        if self.project_root:
            print(f"Target project: {self.project_root}")
        return 0


def _get_fastmcp_backend() -> tuple[Optional[type], Optional[str]]:
    try:
        from fastmcp import FastMCP  # type: ignore

        return FastMCP, "fastmcp"
    except Exception:
        pass

    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore

        return FastMCP, "mcp"
    except Exception:
        return None, None


def _register_tools(app: Any) -> None:
    from .tool_registration import register_all_tools

    register_all_tools(app)


def create_app() -> Any:
    backend_cls, _ = _get_fastmcp_backend()
    if backend_cls is None:
        return _FallbackServer()

    instructions = (
        "Call renforge_info first: its active_project (see project_source — "
        "dashboard, serve_default, or cwd) is the project_path to pass to the "
        "other tools. If active_project is null, ask the user for the game's "
        "path; every tool accepts project_path directly. To play, use the play "
        "loop, which has one tool per task: renforge_session(action=launch) and "
        "poll action=status until ready; renforge_observe to see the screen; "
        "renforge_act with that observation_id and a control id, key/<name>, or "
        "no id to continue (it then progresses to the next decision and returns "
        "the next observation); renforge_explain to read the code behind a "
        "control, label, or screen; renforge_session(action=save|load|stop). "
        "Sessions use isolated saves and preferences. renforge_mod (mod "
        "toolset) writes and reloads a mod. The other tools (live, debug, editor, project, "
        "translation, build, docs toolsets) remain available for existing "
        "workflows and the Live Editor; RENFORGE_TOOLSETS limits which are "
        "listed. RENFORGE_POLICY defaults to off; when set to enforce, "
        "destructive and open-world operations (eval, quit, load, "
        "reload_script, mod writes, and matching scenario steps) require "
        "authorize=true or an allowlist."
    )
    try:
        app = backend_cls("renforge", instructions=instructions)
    except TypeError:  # pragma: no cover - compatibility with older MCP backends
        app = backend_cls("renforge")
    _register_tools(app)
    return app


def run_server(project_root: str | None = None, transport: str = "stdio") -> int:
    app = create_app()
    normalized = Path(project_root).expanduser().resolve() if project_root else None
    if isinstance(app, _FallbackServer):
        if normalized is not None:
            app.project_root = normalized
        return app.run()

    if normalized is not None:
        app.project_root = normalized  # type: ignore[attr-defined]

    runner = getattr(app, "run", None)
    if not callable(runner):
        return 0

    try:
        result = runner(transport=transport)
    except TypeError:
        try:
            result = runner()
        except Exception as exc:
            raise RuntimeError(f"Failed to run MCP server: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to run MCP server: {exc}") from exc

    return 0 if result is None else int(result)
