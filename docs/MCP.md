# RenForge MCP

RenForge is a [Model Context Protocol](https://modelcontextprotocol.io/) server
for Ren'Py projects. An agent can inspect a project, start a game, observe a
frame, click a control, and verify runtime state without guessing the project's
structure or screen coordinates.

## Installation

The MCP server uses `stdio` and starts with:

```bash
uvx renforge@latest serve
```

The `@latest` suffix makes `uvx` fetch the newest published release on each
start instead of reusing a cached older build, so new tools appear without a
manual `uv cache clean`. Use `uvx renforge serve` to pin the cached build.

For a persistent install:

```bash
# Full — MCP + CLI + web dashboard
pipx install "renforge[ui]"

# Slim — MCP server + CLI only (no dashboard deps)
pipx install renforge
```

Then run `renforge serve` (or `renforge ui` with the full install).
`[ui]` is optional: skip it when the stdio MCP server / CLI is all you need.

To develop RenForge:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[ui,test]"
renforge serve
```

The dashboard is a separate process:

```bash
renforge ui --project /path/to/game
```

When the dashboard is running, a `renforge_launch` request from an MCP client
is delegated to it. This is particularly useful with WSLg or when the MCP
client does not have direct access to a graphical display.

## Client configuration

The command is the same for every client. Example JSON configuration:

```json
{
  "mcpServers": {
    "renforge": {
      "command": "uvx",
      "args": ["renforge@latest", "serve"]
    }
  }
}
```

High-risk live tools are classified at invocation time. The default is
`RENFORGE_POLICY=off` (record only). To enforce denials, set
`RENFORGE_POLICY=enforce` in the MCP server environment and pass
`authorize=true` on destructive or open-world calls, or see
[POLICY.md](POLICY.md).

For Codex CLI:

```bash
codex mcp add renforge -- uvx renforge@latest serve
```

Or add this to `~/.codex/config.toml`:

```toml
[mcp_servers.renforge]
command = "uvx"
args = ["renforge@latest", "serve"]
```

For Claude Code:

```bash
claude mcp add renforge -- uvx renforge@latest serve
```

For VS Code (GitHub Copilot), in `.vscode/mcp.json` inside your workspace:

```json
{
  "servers": {
    "renforge": {
      "command": "uvx",
      "args": ["renforge@latest", "serve"]
    }
  }
}
```

For Zed, in `settings.json`:

```json
{
  "context_servers": {
    "renforge": {
      "source": "custom",
      "command": "uvx",
      "args": ["renforge@latest", "serve"]
    }
  }
}
```

> **Windows / GUI clients:** Desktop apps (Claude Desktop, Cursor, etc.) may not
> inherit your shell `PATH`. If `uvx` is not found, set `command` to the
> absolute path of `uvx` (for example `C:\Users\you\.local\bin\uvx.exe`).

> Don't have `uv`? Run `pipx install renforge`, then replace `uvx renforge@latest`
> with `renforge` in your config (and `pipx upgrade renforge` to update).

Every project-related tool takes `project_path`. Call `renforge_info` first
when possible: it returns `active_project` with a `project_source` explaining
how it was resolved — the dashboard selection, the `serve --project` default,
or a Ren'Py project auto-detected from the current directory. A null
`active_project` only means auto-discovery found nothing: every tool still
accepts `project_path` directly (no dashboard required), so ask the user for
the game's path and continue.

`renforge_info` / `renforge_context` also advertise a structured `live_editor`
capability: the Live Editor is **enabled by default** on `renforge_launch`,
with a short public-tool `agent_workflow` and a pointer to
[docs/LIVE_EDITOR.md](LIVE_EDITOR.md). Use only public MCP tools with the
editor — never private test-only `editor_task0_*` handlers.

## Runtime compatibility

Runtime launch requires **Ren'Py 8.0 or newer**. A distributed game's engine
record in `renpy/vc_version.py` and the selected SDK are checked before SDK
download, bridge injection, or process launch. If either is below 8.0, launch
returns `code="UNSUPPORTED_RENPY_VERSION"`,
`phase="checking_runtime_compatibility"`, and details containing the detected
version, selected version, minimum version, and incompatibility source. The agent
should stop for that game. A `version` that is neither `stable` nor a
semantic version such as `8.3.7` returns `code="INVALID_RENPY_VERSION"` in the
same phase. Selecting a newer SDK does not bypass a legacy game's
engine requirement. A 7.5/8.0 build that lists both engines counts as 8.x
only when it ships a Python 3 runtime (`lib/py3-*`); with only `lib/py2-*` its
own launcher runs the Python 2 engine its scripts are written for, so it is
refused. With `version="stable"` (the default) the SDK follows the game's own
engine: a `renpy_version` pin in the project's `renforge.json`
(`{"renpy_version": "8.3.7"}`, a file Ren'Py never reads, for source
projects), else the shipped engine's version, else the
`game/script_version.txt` that Ren'Py writes into builds. Projects with none
of these use the normal configured stable SDK, which must also be 8.0 or newer. The same restriction
applies to autopilot and the low-level bridge launcher.

## Save isolation

Isolation is **opt-in**. Pass `savedir="temporary"` to a launch, or set
`RENFORGE_ISOLATION=temporary` for every launch of a server, and the game does
not read or write the user's normal Ren'Py saves, preferences, or
`$HOME/.renpy`. Without either, launches use the game's normal locations, as
before. The ready payload from
`renforge_launch` / `renforge_launch_status` includes `session_id` and an
`isolation` object (`savedir_mode`, `home_mode`, `persistent_mode`,
`preferences_mode`, resolved paths, and `exposes_user_saves`). A reused game
that another process started has no `session_id`; its `isolation` holds what
its bridge reports (`savedir`, `home`, `save_locations`, `exposes_user_saves`),
and it counts as isolated only when HOME and every save location are inside a
RenForge session root. Copy selected host slots into the isolated session;
never bind-mount the user tree.

There is no MCP-protocol isolation flag and no Roots-based save exposure.
Isolation is tool arguments and the `RENFORGE_ISOLATION` server default, plus
host enforcement (`RENFORGE_POLICY=enforce`, see Recipe 3).

`renforge_session(action="launch")` always isolates: its saves and
preferences live in the project's RenForge state folder (kept across
launches, never the player's), and HOME is a private one. Its `save`,
`load`, and `list` act only on a game whose saves and HOME are in RenForge's
own folders (the state folder or a session root, by what the game's bridge
reports); a game started some other way on the player's saves, or in a
folder outside RenForge's, is refused with `code="SESSION_NOT_ISOLATED"`
before any slot is touched or listed. Use `renforge_saves` for such a game.
A game whose bridge does not answer (busy, or the connection dropped) gets
the ordinary error instead, so the call can be retried, and a failed `list`
carries no `player_slots` or `player_slots_hidden`.

All bridge launch paths, including the low-level Python launcher, resolve
isolation the same way. One disposable session root owns saves, multipersistent data,
HOME, XDG directories, and temporary files. A `python early` adapter installs a
single Ren'Py save location before persistent/preferences load and keeps it on
subsequent storage initialization and script reload. It excludes the original
`game/saves` and `config.extra_savedirs`; `--savedir` alone does not exclude them.
The adapter uses Ren'Py's file storage and scan lifecycle and is tested against
the supported 8.5.3 SDK. SDK upgrades must pass that real-engine contract.

Persistent data and preferences are isolated through storage selection, not
through deletion or a late reset. Their `auto` modes follow the save directory:
a fresh temporary directory starts empty, while `existing` and explicit paths
retain their state. Requesting `empty` with a reused save directory is rejected
before files are touched. The former marker-only `copy` and `fixture` persistent
modes are removed; import selected save slots explicitly instead.

This is game-state isolation for ordinary Ren'Py operations. It adds no OS
sandbox, namespace, mount, network restriction, or arbitrary-Python containment.
Project source, bytecode, caches, and logs still use the project directory.

### Recipe 1 — isolated session

```text
renforge_launch(project_path=project, savedir="temporary")
  -> session_id, isolation.exposes_user_saves=false
renforge_saves(project_path=project, action="list")
  -> slots in the disposable session savedir only
```

### Recipe 2 — copy one or more user saves into the session

```text
renforge_saves(project_path=project, action="list_user")
  -> host slots under ~/.renpy/<config.save_directory> and game/saves
renforge_launch(project_path=project, savedir="temporary")
renforge_saves(project_path=project, action="import", slots=["1-1", "branch-a"])
  (or pattern="1-*" to take every slot whose name matches the glob)
  -> copies those slot files into the isolated savedir
renforge_saves(project_path=project, action="load", slot="1-1", authorize=true)
```

`list_user` writes nothing, needs no running game, and lists nothing from a
save folder swapped for a link. `import` copies files (`.save` and
`.save.json`), and is refused unless the session was launched with
`savedir=temporary`.
`list_user` filters only by `pattern`, a glob matched against the whole slot
name (`1-*`, `auto-?`);
`import` selects slots by name (`slot`, `slots`), by `pattern`, or both.
`list_user` and `import` read the player's own saves, so under
`RENFORGE_POLICY=enforce` both need `authorize=true`, as `load` does.

Import failures carry a code: `NO_ISOLATED_SESSION` (no running session, or it
stopped), `SAVE_IMPORT_NOT_ISOLATED` (the session was not launched with
`savedir=temporary`), `SAVE_IMPORT_INVALID` (malformed or oversized `slot`,
`slots` or `pattern`), `NO_MATCHING_SAVE_SLOTS` (nothing was imported; see
`missing` and `refused`), `SAVE_IMPORT_FAILED` (the savedir was unusable, for
example swapped for a link, or a copy failed part-way), and
`PROJECT_PATH_UNAVAILABLE`. When a dashboard owns the session the import runs
there, and its answer, code included, is relayed unchanged; only a dashboard
that cannot be reached gives `DASHBOARD_REQUEST_FAILED`.

### Recipe 3 — run against the user's save tree (the default)

```text
renforge_launch(project_path=project, authorize=true)
```

Without `savedir="temporary"` the game reads and writes the user's normal saves,
as launches always have (unless `RENFORGE_ISOLATION=temporary`). That reaches
the user's save tree, so under `RENFORGE_POLICY=enforce` it is destructive and
needs `authorize=true`; so does a `savedir` path with `home` omitted (it keeps
the user's HOME), and a `savedir` or `home` path outside the RenForge state
folder (the shared temp dir included), since the game writes saves,
persistent data, and config there. The same isolation knobs, and the same
rule, apply to `renforge_jump` and `renforge_new_game`: the policy judges the
request, so a jump or new game with no isolation options needs
`authorize=true` even when it would inherit an isolated game's isolation.
Pass `savedir="temporary"` to stay isolated without `authorize`.

A running game is handed to a launch only when it matches: it may not use the
user's saves or HOME where the launch isolates them (checked per component),
a launch that needs no authorization gets only a game whose saves and HOME
are in RenForge's own folders (the policy asks the same question), and a
server whose toolsets run no code never gets a bridge that does
(`SESSION_EVAL_MISMATCH`).

Sessions with isolated preferences (`savedir=temporary`, and play sessions)
also start with scene transitions turned off, so dissolves and fades never
leave half-drawn frames to wait out. Set `RENFORGE_TRANSITIONS=on` on the
server to keep them. A session using the player's own preferences is never
changed.

## Where RenForge keeps its files

RenForge never writes into a game's folder except for the bridge script it
injects under `game/` while a session runs (removed on stop) and mods written
on purpose under `game/mods/`. Everything else (activity log, bridge control
files, captures, scene snapshots, autopilot coverage, packaged mods, editor
transactions) goes to a per-project state folder, `<state>`:

```
$RENFORGE_STATE_DIR/projects/<name>-<hash>/       # when set
~/.local/state/renforge/projects/<name>-<hash>/   # default ($XDG_STATE_HOME)
%LOCALAPPDATA%\renforge\projects\<name>-<hash>\   # Windows
```

`<hash>` comes from the project's resolved path, so two copies of a game get
separate folders. An optional `<project>/.renforge/policy.json` is still read
from the project, because the project owner writes it.

## Toolsets

Every tool belongs to one group. `play` is the agent loop, with one tool per
task: `renforge_info`, `renforge_session`, `renforge_observe`, `renforge_act`,
and `renforge_explain`. `mod` holds `renforge_mod`, which writes and reloads
game code, so a server limited to `play` cannot run code in the game.
`renforge_info` is listed whichever groups are chosen, because every workflow
starts with it. The other groups keep every earlier capability for existing
clients, scripts, and the Live Editor:

| Toolset | What it holds |
| --- | --- |
| `mod` | `renforge_mod`: write, lint, reload, remove, and package mods |
| `live` | Launch/stop/saves, advancing, clicks, keys, choices, UI listings, screenshots and comparing them (including `renforge_estimate_translation`, the image-shift estimate), the scene tree |
| `debug` | Game state, variables, `eval`, events, errors, waits, scenarios, autopilot |
| `editor` | The Live Editor |
| `project` | Static project analysis, lint and shadow validation (`renforge_validate`), references, assets |
| `translation` | Languages, dialogue export, translation generation and stats |
| `build` | Web build and distribution |
| `docs` | Ren'Py documentation search |

`RENFORGE_TOOLSETS` (comma-separated, default `all`) chooses which groups the
server lists. Unknown names, or a value that names no group (`,`), stop the
server at startup.

```bash
RENFORGE_TOOLSETS=play renforge serve          # the agent loop only; no code runs in the game
RENFORGE_TOOLSETS=play,mod renforge serve      # plus writing mods
RENFORGE_TOOLSETS=play,debug renforge serve    # plus state inspection
```

## Recommended workflow

To play a game, use [Look, then act](#look-then-act). The workflow below is
for the Live Editor and the earlier `live` tools.

```text
renforge_info
  -> active_project, live_editor (enabled_by_default, launch_tool, guide)
renforge_launch(project_path)
  -> ready, or starting after at most 20 seconds
  -> Live Editor injected by default (floating RF control in the game window)
renforge_launch_status(project_path)  # while starting
  -> starting, ready, failed, closing, closed, or idle
renforge_screenshot(project_path)          # observe the frame (and editor chrome)
renforge_scene_tree(project_path)          # structured layout in logical coords
renforge_game_state_compact(project_path)  # once ready
  -> current label and bounded state
renforge_game_state(project_path, include=["metrics", "audio"])
  -> optional render/cache/window metrics and per-channel audio state
renforge_inspect_screen(project_path, name="say")
  -> active layer, JSON-safe scope, and passed screen arguments
renforge_list_ui_elements(project_path)
  -> visible controls and frame_id
renforge_editor(project_path, action="status")
  -> selected_widget_id, lock_reason, capabilities, frame_id,
     selected_source_position, preview_position
renforge_editor(project_path, action="select", x=..., y=..., expected_frame_id=frame_id)
  -> overlay hit path (not a game click)
renforge_editor(project_path, action="save", dry_run=True)
  -> unified source diff; no transaction, lint, publish, or reload
renforge_editor(project_path, action="save")
  -> overlay Save: reload + attest, or the lock code
renforge_hover_element(..., expected_frame_id=frame_id)
  -> move over a control without clicking
renforge_click_element(..., expected_frame_id=frame_id)
  -> safe interaction (or renforge_click_at with coordinate_space + frame_id)
renforge_stop(project_path)
  -> clean session teardown
```

Live Editor human and agent detail (editable vs locked, Save vs preview,
source safety): **[LIVE_EDITOR.md](LIVE_EDITOR.md)**.

`renforge_launch(language="schinese")` sets `RENPY_LANGUAGE` for a fresh game
process. Ren'Py's `change_language` writes `preferences.language`, so the
override changes the stored preference used by later launches rather than one
session. For verification, isolate state with `savedir="temporary"` (plus
`persistent="empty"`, which requires an isolated savedir) or restore the
original language afterward. It cannot change an already-running session, and
only `renforge_launch` accepts a language: a later jump/new-game/Warp relaunch
starts without it and returns to the stored preference. Launch results echo
`requested_language`, plus `active_language` read from the bridge when the
session is ready. `language_applied` is `true` only when the bridge reports the
requested language active, `false` when the launch did not apply it, and `null`
when the bridge did not report a language. A reused session that cannot switch
reports `false` with a message, unless the language was already active. A
successful launch with `language` and no isolated `savedir` adds a `warning`,
because the stored preference changed.

After editing a running project's `.rpy` file, hot-reload it and inspect the
result without restarting the game process:

```text
edit game/script.rpy
renforge_control(project_path, action="reload_script")
renforge_screenshot(project_path)
```

For image-driven interaction:

```text
renforge_find_image_on_screen(project_path, template_path)
  -> matches[], coordinate_space="screenshot", frame_id
match = matches[0]
renforge_click_at(
  project_path,
  x=match.center.x,
  y=match.center.y,
  coordinate_space="screenshot",
  expected_frame_id=frame_id,
)
```

`frame_id` protects against clicking a stale frame. If the game changes between
observation and click, RenForge returns a guard error and the agent must inspect
the current frame before trying again.

`renforge_list_ui_elements` returns Ren'Py logical coordinates. Bounds returned
by `renforge_find_image_on_screen` use the captured PNG's coordinates, so pass
its `coordinate_space: "screenshot"` to `renforge_click_at`. RenForge converts
them to logical coordinates, including when WSLg scales the capture.

## GOD-mode edit and branch check

Use real labels and visible choice text from the project in this compact
launch-to-diagnostics loop:

```text
project = "/path/to/game"
renforge_launch(project_path=project)
edit game/script.rpy
renforge_control(project_path=project, action="reload_script")
renforge_wait_until(project_path=project, label="edited_label", timeout=30.0)
renforge_screenshot(project_path=project)
renforge_saves(
    project_path=project,
    action="save",
    slot="branch-a",
    extra_info="after hot reload",
)
renforge_list_choices(project_path=project)
renforge_select_choice(project_path=project, text="Branch B")
renforge_wait_until(project_path=project, label="branch_b", timeout=30.0)
renforge_get_errors(project_path=project)
```

`renforge_screenshot` returns the current frame as an image. Use
`renforge_capture_screenshot(name="idle")` when a PNG path is needed for a
later diff or translation estimate. The save call creates a named checkpoint
before selecting Branch B; use
`renforge_saves(project_path=project, action="load", slot="branch-a")` to return
to it. A successful load reply includes `restored_label` so the agent can
attribute the restored state before polling again. `renforge_wait_until`
accepts exactly one of `label`, `screen`, or `expr`, and `timeout` is capped at
120 seconds.

## Pixel-perfect placement

Positioning a sprite, CG, or overlay to the exact pixel is otherwise a blind
loop: edit the `.rpy`, relaunch, eyeball the offset, repeat. These tools let an
agent *measure* instead of guess, then converge on coordinates live before
writing them back to the script.

```text
renforge_get_displayable_bounds(project_path, tag="eileen")
  -> bounds, center, coordinate_space="logical"
renforge_position_element(project_path, tag="eileen", xpos=960, xanchor=0.5)
  -> new bounds after the nudge (tag keeps its attributes)
renforge_screenshot(project_path, grid=100, rulers=True)
  -> a frame with a labelled coordinate grid to read positions off
renforge_diff_screenshots(project_path, before_path="before.png")
  -> bounding box of every pixel that changed vs the live frame
```

Notes:

- `renforge_position_element` re-shows an already-visible tag through a
  `Transform`, so it needs the game running and the tag on screen; it keeps the
  current image attributes (`show eileen happy` stays happy). It reports the
  bounds the tag actually rendered at, not the values you requested.
- Positions follow Ren'Py's own rule: an **integer** is absolute pixels
  (`xpos=600` is 600px) and a **float** is a fraction of the screen
  (`xpos=0.5` is the centre). Send `xpos=600`, not `xpos=600.0`, for pixels.
- `renforge_get_displayable_bounds` and `renforge_position_element` work in
  Ren'Py **logical** coordinates — the same space as `xpos`/`ypos` in a script,
  so measured values drop straight into the `.rpy`.
- Screenshot overlays (`grid`, `rulers`, `crosshair_x`/`crosshair_y`) are drawn
  in the captured image's pixel space. Capture at the game's logical resolution
  (pass `width`/`height`) so the labels read as logical coordinates.
- `renforge_diff_screenshots` needs same-size frames; `threshold` (0..255)
  absorbs anti-aliasing jitter so only real movement is reported.
- `renforge_get_ui_element_bounds` measures the focus rectangle from
  `renforge_list_ui_elements` and, when the control is an `ImageButton`, the
  alpha-painted bounds of its active state. Use `painted_bounds` as the default
  region for `renforge_estimate_translation` when available; otherwise pass an
  explicit region from `focus_bounds`.
- Translation search is bounded to protect the MCP process. If a large region
  and `max_shift` exceed the work budget, tighten the region around the painted
  content or reduce `max_shift`.

## Whole-scene perception

`renforge_scene_tree` turns the rendered frame into structured data so a
non-multimodal agent can reason about layout without a screenshot. Unlike
`renforge_list_ui_elements` (focusable controls only), it walks every layer and
the active screens, reporting sprites, backgrounds, controls, and text blocks in
logical coordinates.

```text
renforge_scene_tree(project_path)
  -> nodes[] (id, type, bounds, center, zorder, screen), window,
     omitted{by_type,by_layer,by_reason}, truncated,
     limits{max_depth,max_nodes,max_text_chars}
renforge_scene_tree(project_path, include=["color","style","overflow"])
  -> per node: composited color.dominant, declared style (with integer
     xmaximum/ymaximum), and on text/button/input nodes only an overflow verdict
     (overflow is true only when the wrapped height exceeds the inner height;
     horizontal wrapping is reported in overflow_axes.x / wrapped.x, not as
     overflow, integer padding/margins are subtracted, vertical text is not
     measured; available:false + reason when no absolute maximum is declared)
renforge_scene_tree(project_path, format="wireframe")
  -> an ASCII map plus a legend mapping labels to ids/bounds/text
renforge_scene_tree(project_path, save_as="before")   # persist a snapshot
# ...edit a screen or advance the game...
renforge_scene_tree(project_path, diff_against="before")
  -> diff{added, removed, changed[moved|resized|text_changed|color_changed|z_changed]}
renforge_measure(project_path, action="align", targets=["quick_menu.Back","quick_menu.Skip"], tolerance=1)
  -> {result:{left,right,top,bottom,center_x,center_y}, pass}
renforge_measure(project_path, action="contrast", targets=["screens/say.text#0"])
  -> {result:{ratio, aa, aaa, fg, bg}}
```

Notes:

- Coordinates are Ren'Py **logical** pixels (the `xpos`/`ypos` space), so measured
  values drop straight into a `.rpy`. A node's `bounds` may be `null` with
  `bounds_available: false` when a displayable cannot be sized.
- The default `detail="semantic"` returns meaningful leaves and always includes an
  `omitted` count of what was filtered, so widen with `detail="layout"|"raw"`,
  `types=[…]`, or `layers=[…]` only when needed.
- If `truncated` is true, `omitted.by_reason` identifies a traversal cap and
  `limits` reports the effective bound; do not treat that response as a complete
  inventory.
- `max_text_chars` bounds node text, screen names, roles, actions, tags, layers,
  and types; truncated values retain up to that many characters and end with `…`.
  IDs use a fixed 256-character cap so they remain stable across output budgets
  and can be passed unchanged to `renforge_measure`.
- The `overflow` verdict uses pixel maximums only: the effective ceiling is the
  tighter of the widget's int `xmaximum`/`ymaximum` (minus integer padding and
  margins) and the child text's own int maximum. The unwrapped width and the
  wrapped height are compared with that box. `overflow` is true only when the
  wrapped height exceeds the inner height, i.e. the text is actually clipped
  vertically; horizontal wrapping is reported in `overflow_axes.x` and
  `wrapped.x`, not as overflow. `overflow_px` is the vertical clip amount. Float
  (relative) maximums are reported but not compared, and vertical text is not
  measured because `Text.size` does not rotate it. A y verdict requires an
  absolute x maximum to know the wrap width; a y-only request is unavailable.
  An x-only result is `complete: false` with `unmeasured_axes: ["y"]` and null
  `overflow`/`overflow_px`.
  Unavailable verdicts keep `overflow` and `overflow_px` as `null` and add a
  reason.
- When a focusable scan hits `max_nodes`, `covered` and `clickable` are `null` with
  `coverage_reason="max_nodes"` because omitted higher controls may overlap them.
- MCP replies default to `max_items=50`, `max_output_depth=6`, and
  `max_output_bytes=65536`; lower these for tighter resource bounds or raise
  them deliberately for a larger inventory.
- `renforge_measure` targets are `scene_tree` node `id`s (resolved live) or literal
  `{x,y,width,height}` bounds; `center` and `fit` also take a `within` container.
- `color` and `contrast` sample the actual composited frame while `style` reports
  declared values, so requesting both surfaces intent-vs-render mismatches.

## ImageButton idle→hover alignment

For `imagebutton` controls with distinct idle/hover art, prefer measuring the
logical shift from `painted_bounds` before relying on a screenshot diff alone.

```text
renforge_list_ui_elements(project_path)
  -> elements[], frame_id
renforge_get_ui_element_bounds(project_path, element_id=id)
  -> focus_bounds, painted_bounds (idle), state="idle"
renforge_capture_screenshot(project_path, name="idle")
  -> path under <state>/captures/
renforge_list_ui_elements(project_path)
  -> current source frame_id
renforge_hover_element(project_path, element_id=id, expected_frame_id=frame_id)
  -> hovered without click
renforge_get_ui_element_bounds(project_path, element_id=id)
  -> painted_bounds (hover), state="hover"
  # dx = hover.painted_bounds.x - idle.painted_bounds.x (logical pixels)
renforge_capture_screenshot(project_path, name="hover")
renforge_estimate_translation(before_path=idle.path, after_path=hover.path, ...)
  # best on named PNG captures; ambiguous on heavily scaled live frames
```

Frame guards are content hashes, not capture counters: an unchanged engine frame
can retain its ID across captures. Refresh after an action or guard refusal. Use
interaction/session/observation guards to detect state changes even when pixels
remain identical.

## Look, then act

The `play` toolset plays a game with one tool per task:

```text
renforge_session(project_path, action="launch")   # then action="status" until ready
loop:
  renforge_observe(project_path)                  # once; every act returns the next observation
  renforge_act(project_path, observation_id, id=...)   # a control, key/<name>, or no id
renforge_explain(project_path, id=... | search=... | label=...)   # when unsure what something does
renforge_session(project_path, action="save" | "load" | "stop", slot=...)
```

Each observation's `forward` says what to do next:

| `forward` | Meaning | Next call |
| --- | --- | --- |
| `dismiss` | A line or soft pause the player would click past | `renforge_act` with no `id` |
| `wait` | A hard pause with no decision controls, which ends by itself | `renforge_act` with no `id` (it waits; nothing is dismissed) |
| `choose` | The game waits for a decision: a menu, editable input, button, modal screen, or hard-pause hub | `renforge_act` with a control `id` (or a key from `keys`) |
| `none` | Nothing RenForge knows moves the game on, including custom input it could not fully discover | Look at `omitted`, `input_candidates`, and the screenshot |

`renforge_observe` returns one compact snapshot:
`observation_id`, stability, dialogue, controls, readout, `forward`, and an
optional screenshot. Defaults and repeats are left out and chrome is reduced to
id and text; `renforge_explain(id=...)` returns a control's full record.
`renforge_act` then takes that `observation_id` and one of:

- a control `id` from `controls` or `chrome` (a chrome id presses the game's own
  interface button, which can save, load, open a menu, or quit), with an
  optional `value`: text for an input field (submitted
  with Enter), a number for a bar, `{"x", "y"}` for a viewport, or a control id
  for a drag's drop target (an empty string drops on nothing);
- `key/<name>` for a physical key (a-z, arrows, escape, return, space, tab),
  with `value` true to hold or false to release (omit it for a tap);
- no `id`, to continue from the observed state.

After the input the game progresses to the next decision (see below) and the
result carries the next observation, so the loop is observe once, then act
repeatedly. Acting on a control in a changed game refuses with
`error="stale"` and returns the current observation. Continuing (no `id`) is
never refused: it cannot choose anything, so it proceeds from the current state
and reports `continued_from_newer_state`. On a line the player could click past
that stopped only for optional buttons (a hotspot or a photo beside the line),
continuing clicks past it (`clicked_past_line`). Buttons on screens kept up
across lines with `show screen` (a phone or codex HUD) never stop such a line,
and an advance that comes round to a line it already passed stops with
`stop="loop"` so the agent can break the loop. When nothing can continue because the game waits
for one of its controls (a hard pause behind a button), the result says so in
`hint`. `keys` lists the key bindings of the screens shown (a screen that continues
on Space, say), each with its action and the `key/<name>` to send when `act`
can press that key. Input controls report what is typed so far as `value`. An unstable snapshot cannot authorize
input on a control.

The earlier `live` toolset still offers `renforge_advance`, `renforge_click_element`,
`renforge_select_choice`, `renforge_send_input`, and the rest, with their own
ids; `renforge_list_ui_elements` reports each element's `control_id`, its id
in `renforge_observe`.

`seen` is true when the current statement (a line, menu, or hub) already
finished once since the game launched; loading a save does not reset it. Lines
in `renforge_act`'s `progress.crossed` transcript carry `seen: true` when they repeat.
Together they show when a playthrough is going in circles, for example a
retry loop after a bad ending.

Controls from `renforge_observe` carry a `source` field: the `file:line` of the
screen-language statement that built them, or null for controls built in
Python. Pass a control `id` to `renforge_explain` to see that statement, its
keyword arguments (such as `action`), and the `if`/`for`/`use` conditions that
enclose it. `renforge_explain` reads the loaded syntax trees and never evaluates
anything, so it is safe to call on a live game.

To check that what is on screen fits the playthrough, `renforge_explain`
also reads state: results for a label, screen, control, or source carry
`values`, the current values of the plain variables their conditions read,
and `variables=["day", "player.day"]` reads named store variables or
dotted fields directly. Pass `variables` alone: combined with `label`,
`screen`, `id`, `source`, `search` or `query` it is refused. It reads at most
100 names; with more, the reply carries `truncated: true`. Game objects come
back as their simple public fields; functions are never called.

`chrome` holds the game's interface: the conventional `quick_menu`, injected
`_renforge_` screens, and buttons whose only actions are Ren'Py interface
actions (opening the game menu, saving, loading, skipping, rolling back,
preferences). When nothing else on the interaction's own screen moves the game
on, those buttons become its controls (a disclaimer whose only buttons are
`MainMenu()` and `Quit()`). Custom overlay screens remain gameplay controls:
overlay registration is a rendering detail, not a reason to ignore a hub.
Readout text does not stop progression. Custom displayables are not fully
inventoried; `omitted` reports focus/traversal limits, unclassified custom
input, and readout truncation. Readout truncation alone does not block
progression. Each binding in `keys` names the `key/<name>` that fires it, or
`act: null` when `act` cannot press it (a function key, or one that needs a
modifier); mouse bindings and interface bindings (menus, rollback) are left
out.

`renforge_session(action="launch")` keeps saves and preferences apart from
the player's (in the project's state folder, so they survive relaunches),
gives the game its own HOME, and leaves the Live Editor out. Calling
`renforge_observe` or `renforge_act` before the game is running returns
`code="NO_RUNNING_GAME"`, or `GAME_STARTING` while it starts. For testing and modding, `label="..."` starts
the story at that label instead: the game leaves its splash screen or main
menu for it, which works for games shipped only as archives. The label is
applied only at startup, so a game this server is running is stopped and
started again for it, and one started elsewhere is refused with
`BRIDGE_PROJECT_LOCKED`. Nothing the
story did before the label has happened (variables keep their defaults, and a
label normally reached with `call` ends the run when it returns), so every
observation carries `started_at_label`; an unknown label is reported as
`start_label_error`. Play from saves when the earlier state matters. To
start from the player's real progress, `action="list"` shows their slots as
`player_slots`, and `action="import", slot="..."` copies one into the
session's saves (the player's file is only read); then `action="load"` it.
The import goes where the running game keeps its saves (a game this server
launched with `savedir='temporary'` takes it in its own folder); a game whose
saves are elsewhere and that this server cannot write into is refused with
`code="SESSION_SAVES_ELSEWHERE"`.
Under `RENFORGE_POLICY=enforce` both need `authorize=true`, since they read
the player's own saves; without it, `list` returns only the session's slots
and says the player's are hidden. `save`, `load`, and `list` answer
`code="SESSION_NOT_ISOLATED"` when the running game is not a session in
RenForge's own folders (for example one launched with `renforge_launch` on
the player's saves).
Ren'Py asks whether to trust a save made on another device: choose Yes for the
save, then No for the device.

```python
snapshot = renforge_observe(project_path=project)
result = renforge_act(project_path=project,
                      observation_id=snapshot["observation_id"],
                      id=chosen["id"])          # or no id to continue
snapshot = result["observation"]
```

`ok` describes whether the action succeeded and `action_outcome` is
`succeeded`, `refused`, `unknown`, or `none` (when continuing). The follow-up is
reported separately in `observation_status`: `ready`, `timeout`,
`transport_error`, `exited`, or `crash`. `action_outcome=unknown` with
`retry_safe=false` marks a lost input reply: observe before deciding whether to
retry. A successful action must not be repeated merely because its follow-up
failed. `timeout` (default 30 seconds) budgets the action and its progression.

Coordinate clicks on unfocused creator-defined displayables hold a synthetic
cursor through a short hover/render interval before mouse-down/up. The bridge
serializes this fallback and replies after clearing its cursor/button state;
a 0.75-second lease and an interaction-generation guard prevent delayed clicks
from starting in a different interaction. Ordinary focused controls keep their
immediate dispatch path. Custom displayables may still have no semantic control
ID. Inspect `input_candidates` and the screenshot, then pass the gameplay
`frame` descriptor with `coordinate_space="screenshot"` for a pixel click. An
empty control list does not prove that no clickable content exists.

Controls advertise the `operations` they support: `click`, `text`, `value`
(bars and viewports), and `drop`. A button runs its action in its screen
context, or receives a pointer click when it has no action. Inputs respect
allow/exclude/length filters. Drops check eligibility and run drag callbacks;
they do not simulate a pointer trajectory. The `live` toolset keeps
`renforge_send_input` for semantic key names that follow the game's keymap.

`renforge_act` progresses with bounded native Ren’Py skip, including unread
narrative. The bridge
pauses skip at every new interaction and resumes only after inspecting a known
narrative interaction. It stops before choices, inputs, custom actionable UI,
or unknown interactions. It never chooses a control automatically. Hard pauses
are allowed to elapse without forced input.

Skip-unseen, skip-after-choices, text-speed, and transition preferences are
changed only for this operation and restored when it stops. An in-engine lease
enforces time and step bounds even if the host disconnects. A stalled main
thread cannot perform cleanup until it resumes; a lost connection reports
`cleanup="lease_expiry_pending"` rather than claiming cleanup succeeded. A
concurrent progression operation or pre-existing manual skip is refused. It
stops at actionable UI, even when dialogue is also visible. The time budget
includes cleanup, final observation, and transcript retrieval; `progress`
reports `stop`, `steps`, `elapsed_ms`, and the crossed dialogue.
Timeouts and transport failures are distinct from verified runtime exceptions.
If the final frame cannot settle, `observation` is null; `last_observation` is
explicitly historical. Stale `act` refusals include a fresh observation when
available, which may itself still be unstable.

When the game raises an exception, Ren'Py shows its error screen. The
observation then carries `game_error` (`message`, the exception line, and
`traceback_file`), progression stops with `stop="error"`, and the screen's
Rollback, Ignore, and Quit buttons are its controls.

Ending detection uses engine evidence. Observations include `lifecycle` with
`phase` (`playing`, `main_menu`, `ended`, or `unknown`) and `ending` (an evidence
object or null). `renforge_act`'s `progress` reports:

- `stop="end"` after a root gameplay script `return` with an empty return stack,
  confirmed by arrival at the engine main menu. `ending` contains a session-local
  monotonic `id`, `reason="script_return"`, and the return's `statement` location.
- `stop="main_menu"` for an initial menu or a menu return without that evidence,
  including `MainMenu`/`renpy.full_restart()` actions. `ending` is null.

The evidence persists across subsequent observations and main-menu Settings,
and clears when root gameplay resumes (including custom `Start(label)` targets),
or on a new start/load/warp/replay. Called-scene returns, nested contexts,
and replays do not establish an ending. Menu detection uses Ren’Py context flags;
custom screen and label names cannot establish completion. A confirmed ending
also emits one `ending` event through `renforge_poll_events`.

This detects the end of an engine run, not completion of every branch or all
available content. Custom endings that remain in gameplay, quit the process,
or explicitly restart without a root `return` do not have confirmed end evidence.
A process exit alone is not a completion signal. When a retained owned process
proves termination, progression reports `exited` for exit code zero or `crash`
for a nonzero exit, with process evidence. A disconnected live process remains a
transport failure. The last reliable observation is retained as historical;
termination never creates ending evidence. If the final frame does not
settle, progression returns `ending=null`; historical observations are not proof
of a current ending.

Gameplay observation images are limited to 384 KiB of PNG data so large scenes do not
routinely exceed MCP event limits. `frame_hash` hashes the exact returned PNG;
`frame_id` identifies the source capture used by engine guards; it can differ
from `frame_hash` after resizing. `frame` carries that source identity, session,
returned image dimensions, source capture dimensions, and logical dimensions.
Pass the complete descriptor to click a pixel in the returned gameplay image:

```python
snapshot = renforge_observe(project_path=project, screenshot=True)
renforge_click_at(project_path=project, x=image_x, y=image_y,
                  coordinate_space="screenshot", expected_frame_id=snapshot["frame"])
```

Descriptor-backed clicks validate session, source frame, dimensions, and image
bounds before input. Bare screenshot-coordinate calls retain their legacy
meaning: pixels in the engine's full capture. Resized, cropped, or annotated
legacy captures do not carry the gameplay descriptor; their delivered PNG hash
and pixels cannot be substituted for a full-capture guard and coordinates. A string guard must be a source
`frame_id`, not a resized image's `frame_hash`. There is no implicit last-image
cache. `screenshot` retains original/returned dimensions and scaling metadata.
Control bounds stay in logical coordinates. Request `screenshot=false` for
text-only progression or observations.

Observation, UI listings, actions, hit tests, and scene inspection share a control
collector. IDs returned by UI listings now use the same canonical IDs as
observation; refresh old listings before acting. Explicit bare widget IDs remain
accepted by the legacy UI resolver, with ambiguity checks and an optional screen
filter. Menu entries are identified by
current engine actions on any active layer, independently of screen names or
caption equality. Duplicate captions remain distinct. `select_choice` refuses
ambiguous text and accepts optional `interaction`, `session_id`, and
`observation_id` guards. Its listed indices retain the broader textual-control
ordering; use the returned index rather than counting menu rows yourself.
A current eligible menu action remains selectable when viewport clipping hides
its button; `semantic=true` distinguishes that operation from a pointer hit.

An active screen absent from the completed render returns `stable=false` with
`unstable_reason="not_ready"`; fetch a fresh observation after it draws.
A timed `pause` of 5 seconds or less returns `stable=false` with
`unstable_reason="pause"` until it ends, because the script moves on by
itself (screens shown under a fade can still be clickable). Longer pauses
are stable and can be dismissed.
Boundary stop reasons reflect the settled final observation; step/time budgets
and failures retain their actual stop reason.
Geometry comes from the completed frame, with transforms and clipping applied.
`bounds=null` and `bounds_reason` explain unavailable geometry; `occurrences`
preserves repeated placements and render paths. Bounds enclose a region and do
not guarantee every enclosed pixel is interactive. `click_point` is a point
proved eligible by bounded engine focus probes. Unproven coverage remains
unknown rather than declaring an entire control covered from its center.
Alpha inspection rasterizes completed renders; it does not re-render game
widgets. Optional natural-text overflow measurements report unavailable when
that size cannot be established from the completed frame.

`input_candidates` reports on-screen custom event handlers whose semantics are
unsupported, including their screen/layer and geometry when established.
Handlers with nothing to point at (hidden, or at most one pixel, such as gamepad
and key listeners) are left out. These regions have
no inferred action, label, sensitivity, or clickability. Visible unresolved
candidates set `omitted.unclassified=true`; both progression modes stop with
`unsupported` and guarded dismiss refuses input. Known engine widgets and
ordinary decorative displayables do not require custom adapters. Conditional
custom handlers and story prerequisites remain unknown. A custom region may
render across the entire frame, so its enclosing bounds can be too broad to
locate a specific hotspot. Use the visible UI and
bounded coordinate input to find it.

`crossed` preserves distinct repeated dialogue occurrences, including rendered
speaker names, and merges callback/frame copies. Events are drained throughout
progression and again after stopping. `crossed_truncated` reports event-buffer
gaps or failed retrieval. A follow-up observation returned by `act` does not
consume the transcript cursor; explicit `observe` and progression do.

## Modding

`renforge_mod` writes a mod into a game without touching the game's own files.
Each mod lives in `game/mods/<name>/` and records every file it writes in
`renforge-mod.json`, so removal deletes exactly what RenForge created.

The loop for an agent:

```text
renforge_mod(project_path=project, action="write", name="demo", path="intro.rpy", content="...")
renforge_mod(project_path=project, action="lint", name="demo")          # shadow-copy lint, diagnostics inside the mod only
renforge_mod(project_path=project, action="reload")                     # hot reload, errors, fresh observation
renforge_observe(project_path=project)                                  # check the result in the running game
renforge_mod(project_path=project, action="remove", name="demo")        # listed files, .rpyc siblings, empty dirs
```

`package` zips the mod into `<state>/mods/<name>.zip`. `list` shows each
mod's files plus any `missing` (listed but gone) or `unlisted` files. Paths must
be relative, use `/`, end in `.rpy`, `.txt`, `.json`, or `.md`, and stay under
1,000,000 UTF-8 bytes. Symlinks under `game/` are refused. `write` will not take
over files it did not create: a `game/mods/<name>/` folder without a manifest
(some games ship their own mods there), or a file (or a `.rpy`'s `.rpyc`) the
manifest does not list, is refused, so choose another name. `remove` also
refuses a mod whose own folder, `game/mods`, or `game` is a link, and never
enters a linked folder inside the mod. `lint` runs
Ren'Py lint on a temporary shadow copy, as `renforge_validate` does, so the
project's `init` code runs but nothing is written in the game. It uses the
game's own engine version, like `version="stable"` elsewhere (the
`renforge.json` pin, the shipped engine, then `script_version.txt`), but never
an SDK found inside the project. When it fails
with nothing in the mod, `raw_tail` holds the end of Ren'Py's output. `guide` returns modding idioms
(`init offset`, screen replacement, `config.label_overrides`, overlay screens).

## Tool catalogue

### Discovery and static analysis

| Tool | Purpose |
| --- | --- |
| `renforge_info` | Version, dashboard status, active project, and the structured `live_editor` capability (enabled by default, launch tool, agent workflow, guide). Call this first. |
| `renforge_context` | Same discovery payload as `renforge_info` (active project + `live_editor`). |
| `renforge_inspect_project` | Lightweight Ren'Py project summary. |
| `renforge_scan_project` | Scan scripts, labels, links, and metadata. Use filters and pagination for large projects. |
| `renforge_find_references` | Exact Ren'Py definitions and usages, including text interpolations. |
| `renforge_parse_lint` | Parse `renpy lint` output. |
| `renforge_validate` | Lint a clean shadow copy of the project with optional staged bytes; reports the resolved `sdk_version`, shadow measurements, quota limits, and skipped linked directories. Media files are zero-byte placeholders, so asset-heavy projects lint without copying media bytes. |
| `renforge_inspect_image` | Inspect a local image, with optional crop and zoom. |

### Game lifecycle

| Tool | Purpose |
| --- | --- |
| `renforge_session` | The play-session lifecycle (`play` toolset): `launch` with agent-mode defaults (isolated saves and preferences, no Live Editor), `status`, `save`/`load`/`list` named slots (`load` returns the restored game's observation; `list` also shows the player's `player_slots`), `import` to copy one of the player's slots in, and `stop`. `save`/`load`/`list` refuse a running game whose saves or HOME are outside RenForge's own folders with `SESSION_NOT_ISOLATED`, so they never touch the player's saves. The tools below remain for existing workflows. |
| `renforge_launch` | Start or reuse a game with the **Live Editor enabled by default** and inject the temporary bridge. After launch, poll `renforge_launch_status`, observe with `renforge_screenshot` / `renforge_scene_tree`, then use guarded clicks — see [LIVE_EDITOR.md](LIVE_EDITOR.md). The call waits at most 20 seconds, then returns `status=starting` while startup continues in the background. A competing launch returns `code=LAUNCH_IN_PROGRESS` instead of discarding its parameters. `warp` accepts `file:line`; `display`/`audio` default to `auto`. Pass `savedir=temporary` (or set `RENFORGE_ISOLATION=temporary`) to **isolate saves, preferences, and HOME** in a disposable folder so the session does not read or write the user's normal Ren'Py state (`~/.renpy` / `game/saves` / host preferences); without it, launches use them as before. Under `RENFORGE_POLICY=enforce`, a launch needs `authorize=true` unless its saves and HOME both stay in RenForge's own folders (`savedir=temporary` with `home` omitted or `temporary`, or state-folder paths for both): that includes the default with no isolation options (unless `RENFORGE_ISOLATION=temporary`), a `savedir` path with `home` omitted, and any `savedir`/`home` path outside the RenForge state folder (the shared temp dir included). Ready payloads include `session_id` and an `isolation` object; a reused game started by another process (another RenForge server or the dashboard) has no `session_id`, and its `isolation` is what its bridge reports. A launch that would be handed a running (or starting) game it may not have is refused instead: `SESSION_ISOLATION_MISMATCH` when that game uses the user's saves or HOME where the launch isolates them, or when the launch needs no authorization and that game keeps its saves or HOME outside RenForge's own folders; `SESSION_EVAL_MISMATCH` when this server's toolsets allow no code but that game's bridge runs it. Use `renforge_saves(action="import")` to copy selected host slots into an isolated session. `language` sets `RENPY_LANGUAGE` for a fresh game process (Ren'Py persists it as `preferences.language`, so it changes the stored preference rather than one session; isolate with `savedir=temporary` or restore the language afterward; it cannot change a running session, and only this tool forwards it, so a later jump/new-game/Warp relaunch uses the stored preference). Isolation is different: a jump/new-game/Warp relaunch, or a `renforge_launch` with `warp`, with every isolation knob omitted or `auto` that replaces a game this server (or its dashboard) owns keeps that game's saves and HOME isolation, resolved by the process that owns the game (unless this server's `RENFORGE_ISOLATION=temporary` already isolates it, in which case this server resolves it); it gets a fresh session, so slots imported into the old one are not carried over. Pass the knobs explicitly to choose otherwise. `timeout` controls the background startup deadline. |
| `renforge_launch_status` | Report `starting`, `ready`, `failed`, `closing`, `closed`, or `idle` for the latest launch. While cancellation is pending it keeps `status=starting` and sets `cancel_requested=true`. Ready payloads repeat `session_id` and `isolation` (only `isolation` for a game started by another process), and report `isolated` and `eval_enabled`. |
| `renforge_jump` | Restart at a label or `file:line`; it uses the same non-blocking launch lifecycle and the same isolation knobs as `renforge_launch`. With every knob left `auto`, it keeps the saves and HOME isolation of the game it replaces, unless this server's `RENFORGE_ISOLATION=temporary` isolates more (a fresh session: imported slots are not carried over). The policy judges the request, not the game it replaces: under `RENFORGE_POLICY=enforce` a jump needs `authorize=true` unless its saves and HOME both stay in RenForge's own folders (`savedir=temporary` with `home` omitted or `temporary`, or state-folder paths for both), so a jump with no isolation options needs it unless `RENFORGE_ISOLATION=temporary`, even when it would inherit an isolated game's isolation. |
| `renforge_new_game` | Start a fresh process at the `start` label through the same non-blocking lifecycle and isolation knobs as `renforge_launch`. With every knob left `auto`, it keeps the saves and HOME isolation of the game it replaces, unless this server's `RENFORGE_ISOLATION=temporary` isolates more (a fresh session: imported slots are not carried over). Under `RENFORGE_POLICY=enforce` it needs `authorize=true` by the same rule as `renforge_jump`: unless its saves and HOME both stay in RenForge's own folders, which a call with no isolation options does only when `RENFORGE_ISOLATION=temporary`. |
| `renforge_stop` | Stop a running game or cancel an in-progress launch. Uses authenticated bridge quit when a session is owned; may return `SHUTDOWN_INCOMPLETE` with deferred cleanup while the process/artifacts finish tearing down, or `ok=false` with `BRIDGE_TEARDOWN_INCOMPLETE` and the `failed` items when the game or its bridge files are still there (the session stays registered, so stop can be retried). When another launch holds the project lock, reports `replacement_running`. An uninterruptible startup phase may return `launch_cancel_requested=true`; poll launch status until it becomes `failed` or `closed`. |
| `renforge_game_state` | Complete state, including variables. Pass `include=["metrics", "audio"]` to add compact render/cache/window metrics and registered-channel audio state. Omitting `include` preserves the default response. Optional `state_profile` filters the store. |
| `renforge_game_state_compact` | Bounded state (`state_profile=interaction` by default); select variables by name or prefix; supports serialization limits. |
| `renforge_advance` | Advance the current dialogue. |
| `renforge_observe` | Read one interaction: identities, readiness, lifecycle/ending evidence, controls, chrome, readout, uncertain input candidates, completeness flags, and an optional bounded PNG with its frame descriptor. Compact; `renforge_explain(id=...)` has the full record. |
| `renforge_act` | The play step (`play` toolset): act on a control id, `key/<name>`, or nothing with the `observation_id` it was chosen from, plus an optional `value`; then progress to the next decision and return `progress` and the next compact observation. |
| `renforge_control` | Run one action: `advance`, `rollback`, `toggle_skip`, `toggle_auto`, `toggle_afm`, `game_menu`, `hide_windows`, `quick_save`, `quick_load`, `reload_script`, `restart_interaction`, or `quit`. Emits correlated business events; `wait_for_effect=true` waits for the matching event. |
| `renforge_send_input` | Send exactly one `text`, named `key`, or logical-coordinate `scroll` operation. Text replaces the contents of a focused Ren'Py `Input` (falling back to character events when needed); `submit=true` presses Enter. Supported keys include `enter`, `esc`, arrows, `pageup`, `pagedown`, `backspace`, `delete`, `home`, `end`, `space`, `tab`, and `f1`-`f12`. Scroll uses `{"x": ..., "y": ..., "direction": "up"|"down", "amount": 1}`. |
| `renforge_saves` | Run `save`, `load`, `list`, `list_user`, or `import` for named slots. `list` reads the running session; `list_user` observes host slots without a running game; `import` copies selected host slots (`slot`, `slots`, or a glob `pattern`) into a session launched with `savedir=temporary` (any other session, including `savedir=<path>`, is refused with `SAVE_IMPORT_NOT_ISOLATED`), so it never copies the player's saves into a folder the caller chose. `list_user` and `import` read the player's own saves, so under `RENFORGE_POLICY=enforce` they need `authorize=true`, like `load`. `import` returns `imported` (`name`, `files`, `directory` per slot), `missing` (requested names with no host slot), and `refused` (`name`, `file`, `reason`) for each slot file it would not copy: `reason` is `link`, `not_regular`, `too_large`, or `unreadable`, and `file` is null when the slot's whole host folder was refused because it was swapped for a link. A slot counts as imported only when its save file was copied (its `.save.json` sidecar is copied only with it), and `ok` is false with `NO_MATCHING_SAVE_SLOTS` when nothing was. Save/load require `slot`; save accepts optional `extra_info`; load returns `restored_label`; `list` accepts an optional `regexp` (applied by Ren'Py) and `list_user` an optional glob `pattern`; both return `name`, `extra_info`, and `mtime` without screenshots. |
| `renforge_screenshot` | Capture a frame; width, height, crop, scale, and `grid`/`rulers`/`crosshair` overlays are optional. Passing only one of `width`/`height` keeps the game's aspect ratio. |

### Mods

| Tool | Purpose |
| --- | --- |
| `renforge_mod` | Write, test, and remove a mod under `game/mods/<name>/`. `action` is `write` (`path`, `content`), `list`, `lint`, `reload`, `remove`, `package`, or `guide`; every action except `list`, `guide`, and `reload` takes `name`. Game files outside that directory are never modified. `write` refuses a folder RenForge did not create and any file the manifest does not list. `lint` lints a temporary shadow copy (the project's init code runs there) and keeps diagnostics in the mod, parse errors included. `remove` deletes only manifest-listed files plus their `.rpyc` siblings, and reports unlisted files under `kept`. `write`, `remove`, and `package` return `files_touched`/`files_deleted`. `reload` hot-reloads the running game and returns `reload`, a compact `observation`, and `errors`. See [Modding](#modding). |

### Choices and user interface

| Tool | Purpose |
| --- | --- |
| `renforge_list_choices` | Current engine menu entries across layers, including clipped choices. Use the returned indices; they retain legacy textual-control ordering. |
| `renforge_select_choice` | Select an eligible menu entry by unique text or listed index. Accepts interaction/session/observation guards; ambiguous captions require an index. |
| `renforge_list_ui_elements` | Supported controls and engine menu actions with canonical IDs, operations, visibility/coverage, logical bounds when available, and a source `frame_id`. A clipped menu action can remain semantically selectable. |
| `renforge_hover_element` | Move the pointer over a control by ID or text without clicking. Supports `exact`, `screen`, and `expected_frame_id`. |
| `renforge_get_ui_element_bounds` | Report `focus_bounds` and, for `ImageButton` controls, rendered `painted_bounds` for the active state. Returns `painted_bounds_available: false` with a reason when the painted content cannot be measured. |
| `renforge_click_element` | Click a control by ID or text. Supports `exact`, `screen`, `expected_frame_id`, `interaction_id`, and `wait_for_effect`. Returns `received_by` when another control owns the hit point. |
| `renforge_editor` | Public Live Editor: `status`, `select` (overlay hit path), and `save` (reload + attest). `save` with `dry_run` returns a unified source diff without writing, linting, reloading, or creating a transaction; it rejects `x`/`y` and older bridges without dry-run support (`DRY_RUN_UNSUPPORTED`). Locked targets return the lock code. Never call private `editor_task0_*` handlers. |
| `renforge_click_at` | Click logical or source-capture pixels; pass `expected_frame_id=observation.frame` to map and guard pixels from a delivered gameplay image. Also accepts `expected_state`. |
| `renforge_hit_test` | Inspect the interactive focus stack at a point (`topmost` + `underneath`) to diagnose overlays that intercept clicks. |
| `renforge_capture_screenshot` | Persist the current frame as a named PNG under `<state>/captures/` and return `path`, `relative_path`, SHA-256, and dimensions for later diff/translation tools. `relative_path` and the `files_touched` receipt are relative to the project only when the capture is inside it; the state folder is normally outside the project, so they then hold the same absolute path as `path`. |
| `renforge_estimate_translation` | Estimate `dx`/`dy` between two saved PNGs with Pillow, returning `confidence`, `support`, and explicit unavailability when the measure is ambiguous. Search work is bounded; use a tighter region or lower `max_shift` when needed. |
| `renforge_find_image_on_screen` | Locate a local PNG template in the current frame and return confidence, bounds, center, and frame guard. |
| `renforge_get_displayable_bounds` | Report the logical bounds and center where a shown image tag was rendered. |
| `renforge_position_element` | Reposition a shown image tag live (`xpos`, `ypos`, anchors, align, offsets, `zoom`, `rotate`) and return its new bounds. |
| `renforge_diff_screenshots` | Diff two frames (or a saved PNG against the live frame) and return the changed region's bounding box. |
| `renforge_scene_tree` | Inspect a bounded scene inventory with completed-frame logical geometry — layer displayables, supported controls, and text — with `id`, `type`, `bounds`, `center`, `zorder`, `screen`, and an `omitted` completeness hint. Beyond `renforge_list_ui_elements` (supported controls/menu actions) it also reports non-focusable text and images. `detail` is `semantic` (default), `layout`, or `raw`; `layers`/`types`/`screen`/`ids` scope it; `include=["color","style","overflow"]` adds composited colour, declared style (including integer `xmaximum`/`ymaximum`), and a text overflow verdict on `text`/`button`/`input` nodes only (`overflow` means actual vertical clipping; horizontal wrapping is reported in `overflow_axes.x`/`wrapped.x`, integer padding/margins are subtracted, vertical text is not measured; absent or relative maximums return `available: false` with a reason); `format="wireframe"` adds an ASCII map; `save_as`/`diff_against` persist and diff snapshots under `<state>/scenes/`. |
| `renforge_measure` | Measure pixel relationships between scene nodes (by `id`) or literal bounds without vision: `align`, `gap`, `distribute`, `center`, `overlap`, `fit`, and WCAG `contrast`. Returns logical-pixel deltas and, with `tolerance`, a `pass` verdict. |

### State and controlled execution

| Tool | Purpose |
| --- | --- |
| `renforge_eval` | Evaluate a Python expression in `store`. Use for diagnosis and development only. Open-world: requires `authorize=true` when `RENFORGE_POLICY=enforce`. |
| `renforge_inspect_screen` | Inspect whether a screen is active and, when shown, return its layer, JSON-safe scope, and passed arguments. |
| `renforge_explain` | Explain where game content is defined and what it does, read from the running game's loaded syntax trees without evaluating anything. Works for `.rpa`/`.rpyc`-only games. No selector returns an index of labels and screens with `file:line` (filtered by a `query` name substring) and file counts; `label` lists a label's statements; `screen` lists a screen's displayable tree; `id` (a control from `renforge_observe`) or `source` (a `file:line` from a control or input candidate) returns the statement there, its keyword arguments, and the enclosing `if`/`for`/`use` conditions; `search` finds text in every loaded statement. Results for a label, screen, id, or source carry `values` (the plain variables their conditions read), and `variables` reads named store variables or dotted fields as data. Bounded by `limit`. |
| `renforge_get_var` | Read a store variable. |
| `renforge_set_var` | Write a store variable. |
| `renforge_poll_events` | Read label, dialogue, confirmed ending, exception, and business events (`quick_save.completed`, `skip.stopped`, …) from a cursor. Events may carry `correlation_id` and `timestamp`. |
| `renforge_get_errors` | Read recent bridge errors or bounded crash-file tails with mtimes and exit code when tracked. |
| `renforge_wait_until` | Wait for exactly one `label`, `screen`, or `expr` condition with bounded `timeout` (maximum 120 seconds) and polling `interval`. Returns compact state by default (`state_profile=interaction`); pass `include` for extra fields and `state_profile=full` only when needed. |
| `renforge_run_scenario` | Execute a multi-step live scenario (`set`, `click`, `wait`, `assert`, …) in one call; captures diagnostics on failure. |
| `renforge_autopilot` | Explore choices backed by active Ren'Py menu `items` across fresh replays, including custom-named and one-item menus; report label coverage and crashes. Custom `call_screen` interactions that do not expose Ren'Py menu `items` are not currently explored automatically. |

### Project, translation, builds, and Ren'Py documentation

| Tool | Purpose |
| --- | --- |
| `renforge_assets` | Find orphaned or missing image and audio assets. |
| `renforge_languages` | List languages under `game/tl/`. |
| `renforge_translation_stats` | Report translation progress and missing strings for one language. |
| `renforge_generate_translations` | Generate or update `game/tl/<language>/`. Writes to the project. |
| `renforge_export_dialogue` | Export dialogue as plain text. |
| `renforge_web_build` | Create a browser build; requires the SDK web DLC. |
| `renforge_distribute` | Build desktop distributions (`pc`, `mac`, `linux`, and more). |
| `renforge_search_docs` | Search the offline Ren'Py documentation. |
| `renforge_get_doc` | Read an offline Ren'Py documentation page. |
| `renforge_list_docs` | List available offline documentation pages. |

`renforge_translation_stats`, `renforge_generate_translations` and
`renforge_export_dialogue` refuse a `language` that is not a Ren'Py language
name (a Python identifier such as `french`) with `code="LANGUAGE_INVALID"`
before running Ren'Py, which joins it into `game/tl/<language>/` unchecked.

### Shadow validation

`renforge_validate` runs `renpy lint` against a **clean shadow copy** of the
project tree. It never publishes to project sources and does not create an
editor transaction. The shadow excludes ignored caches and compiled files, and
omits RenForge-injected session artifacts: manifest names from
`<state>/control/artifacts.json` when that manifest is available, but only
when they carry the known `zzrenforge_*` or `00renforge_session_*` prefixes. A
crafted or stale manifest therefore cannot hide user source. The result
describes this clean tree, not a live session.

`staged` overlays UTF-8 text into the shadow without touching the project. Keys
may be project-relative (`game/script.rpy`) or game-relative (`script.rpy`);
absolute paths, `..`, and duplicate normalized paths are rejected. Staged bytes
count against the shadow quotas.

Shadow quotas default to **10,000 files**, **500 MiB**, and **50,000
directories**. Media files
(images, audio, fonts, and video) are represented by **zero-byte placeholders**
by default, so an asset-heavy project lints without copying media bytes; lint
resolves names, not contents, and `init python` code that opens media bytes sees
empty files. Media placeholders count as files but not bytes. Other files are
copied and count normally. Set `RENFORGE_SHADOW_MEDIA=copy` when a caller needs
a faithful shadow: media bytes are then copied and count toward the byte quota.
The result reports `placeholders`, the number of files materialized by name
only. Override the limits with positive integers:

```bash
RENFORGE_SHADOW_MAX_FILES=25000
RENFORGE_SHADOW_MAX_BYTES=2147483648  # 2 GiB
RENFORGE_SHADOW_MAX_DIRS=100000
```

Invalid values fail closed with `SHADOW_QUOTA_INVALID` rather than silently
using the defaults. A quota breach returns `SHADOW_QUOTA_EXCEEDED` with
`files`, `bytes`, `directories`, `max_files`, `max_bytes`, `max_dirs`, the
`offending_path`/size that crossed the ceiling, and the `largest_path`/size
top-level contributor (`game/images`, `game`, or `.` for project-root files).
File and byte totals cover the whole walked tree, so they remain accurate after
the first entry that crosses a limit. A staged replacement can shrink the final
byte total, so the byte verdict is taken after applying staged text; the file
and directory verdicts cannot be reduced by a staged replacement.

Linked directories (symlinks/junctions) are **never copied byte-for-byte**.
Each skip is recorded in `skipped_links` with a reason. A linked directory under
`game/` (matched case-insensitively) is scanned read-only for `.rpy`/`.rpym`
source: the scan never reads file contents, is capped at 50,000 entries, and
checks a 5-second budget between entries. A hung network/UNC operation may still
block until the OS call returns. If any
source is found the reason is `linked_directory_with_source` and
`complete=false` (and `ok=false`). If the target cannot be fully scanned (broken
link, nested link or special file, unreadable subtree, entry cap, or timeout)
the reason is `linked_directory_uninspectable` and `complete` is also false.
When the target is fully scanned and contains no source, the shadow mirrors its
directory structure and creates **zero-byte placeholder files** for its regular
files, so lint still sees the expected image/audio names without copying any
bytes; the reason is `linked_directory_assets_placeholders`, `complete` stays
true, and `degraded=true`. Placeholders count as files but contribute zero
bytes to the quota, and none are created for `.rpy`/`.rpym` or compiled-artifact
suffixes. Links outside `game/` cannot affect source lint, so they never clear
`complete`; their reason is `linked_directory_not_followed`. Linked regular
files and other special files remain fatal (`SHADOW_SPECIAL_FILE`). The Live
Editor commit and undo gates refuse a shadow with skipped source directories
(`SHADOW_SOURCE_INCOMPLETE`); a source-free linked asset directory stays
eligible because its placeholders let lint validate references.

`checks` currently accepts only `["lint"]`. `version` selects the Ren'Py SDK:
`stable` (default, honoring `RENPY_SDK_STABLE_VERSION`) or an explicit version
such as `8.5.3`; the result reports the resolved `sdk_version`. `timeout=0`
derives
`min(180, max(1, attestation_timeout*3))` (90 seconds with the default 30-second
attestation budget); explicit values are clamped to 1-600 seconds. Lint runs
with `--error-code`, so reported lint errors produce a non-zero return code.
`ok` is false for a timeout. `renforge_validate` also reports `ok=false` for a
truncated transcript, because it always uses `--error-code` and a report it
cannot show is not a verdict it can return. Reported lint errors fail the tool.
The Live Editor commit/undo gate keeps the legacy exit code by default so
pre-existing lint errors do not block unrelated edits; in that lenient mode a
long transcript is recorded as `truncated` but does not fail the commit by
itself. Set `RENFORGE_SHADOW_STRICT_LINT=1` to make that gate strict as well. Set
`keep_shadow=true` to retain the temporary tree and receive its
`shadow_path` (the caller owns cleanup). The tool may download and cache the
Ren'Py SDK. It uses the requested `version` or the managed cache, never a
launcher discovered inside the project being validated: a project-controlled
fake SDK must not become executable code. Linting is not a sandbox: like
`renforge_launch`, `renpy lint` executes the project's `init python` blocks with
the current user's privileges, so validating a project you would not run runs
its code. The shadow isolates writes, not execution. Project sources are unchanged, but runtime state may be created:
the tool wrapper appends `<state>/activity.jsonl` (see
[Where RenForge keeps its files](#where-renforge-keeps-its-files)). Errors use the standard tool shape:
`ok=false` with a string `error` and top-level `code`/`details`.

## Writes and safety

These tools change game or project state: `renforge_session`,
`renforge_launch`, `renforge_jump`, `renforge_new_game`, `renforge_stop`,
`renforge_act`, `renforge_advance`, `renforge_control`,
`renforge_send_input`, `renforge_saves`, `renforge_mod` and
`renforge_editor` (both write source files),
`renforge_select_choice`, `renforge_click_*`, `renforge_run_scenario`,
`renforge_position_element`, `renforge_set_var`,
`renforge_generate_translations`, `renforge_web_build`, and
`renforge_distribute`.

`renforge_validate` does not publish to project sources: it builds a temporary
clean shadow and never records an editor transaction, but runtime state and the
SDK cache may be created (the wrapper's `<state>/activity.jsonl` and the
downloaded SDK cache). This guarantee covers
RenForge, not the project's own code: lint executes `init python` with the
caller's privileges, so a project that writes an absolute path can still
change files.

`renforge_send_input` is also stateful: supply exactly one mode per call. Text
input fails explicitly when Ren'Py cannot verify a focused `Input`, so refresh
the screen or focus the field before retrying; a successful response means the
events were queued on the game thread, not that an unrelated screen consumed
them.

`renforge_game_state` is read-only. Its optional `include` list accepts only
`metrics` and `audio`; an unknown value is rejected so a typo cannot silently
change the response. Metrics report `render_time_ms`, an FPS estimate,
`image_cache_size`, and logical/physical window sizes. Audio reports every
registered channel with its playing filename, volume, and pause state when
Ren'Py exposes them.

Running code in the game (`renforge_eval`, `renforge_set_var`, and the
conditions of `renforge_wait_until` and `renforge_run_scenario`) needs the
bridge's `eval`. A server whose toolsets include neither `debug` nor `editor`
(`RENFORGE_TOOLSETS=play`, say) launches games whose bridge refuses it
(`eval_disabled`); reading variables and the play loop are unaffected.
`renforge_mod`, in the separate `mod` toolset, writes scripts on purpose;
under `RENFORGE_POLICY=enforce` writing one needs `authorize=true`.

`renforge_inspect_screen` is read-only and reports `active=false` clearly when
the requested screen is not shown. There is intentionally no separate style
tool yet: use `renforge_eval` as the controlled style-introspection escape
hatch when the active screen's resolved style needs investigation. Treat
`renforge_eval` as arbitrary Python execution and use it only on a trusted
local project. When `RENFORGE_POLICY=enforce`, `renforge_eval`, destructive
`renforge_control` / `renforge_saves` / `renforge_session` actions, matching
`renforge_run_scenario` steps (including every `assert` and every `wait` on
an `expr`), and every `renforge_launch`, `renforge_jump`, or
`renforge_new_game` whose saves and HOME do not both stay in RenForge's own
folders are denied unless `authorize=true` or an allowlist permits them. For
launches that means: no isolation options (they use the user's saves and HOME
unless `RENFORGE_ISOLATION=temporary`, and a jump or new game is judged this
way whatever game it replaces), `savedir=existing` or `home=existing`, a
`savedir` path with `home` omitted (it keeps the user's HOME), and any
`savedir`/`home` path outside the RenForge state folder. Pass
`savedir="temporary"` (with `home` omitted or `"temporary"`) to launch an
isolated game without `authorize`; a denied launch's `policy.next_step` says
so first. See [POLICY.md](POLICY.md) for the operation-level model, its
relationship to MCP `ToolAnnotations`, and the compatibility plan, and
[Save isolation](#save-isolation) for what each option isolates.

`renforge_scene_tree` is read-mostly: it never changes game state, but `save_as`
writes a JSON snapshot under `<state>/scenes/` for later diffing.
`renforge_measure` is read-only. Requesting `include=["style"]` on
`renforge_scene_tree` is the structured alternative to the `renforge_eval` style
escape hatch above.

### Write receipts

Mutating tools that can enumerate their outputs report a write receipt:
`files_touched` lists the files or directories written, and cleanup tools also
return `files_deleted`. A cleanup that fails part-way still lists the paths that
are actually gone, and `failed` names the resource it could not finish. Paths inside the project are project-relative POSIX
(for example `game/screens.rpy`, `game/tl/french`, or `dialogue.txt`); paths
outside the project (an external build destination or Ren'Py's savedir) stay
absolute. A tool that cannot enumerate its outputs — `renforge_launch`, lint
analysis, a failed build or translation, or a build that used Ren'Py's default
destination because no `destination` was passed — omits the key instead of
returning an empty list, so an empty list always means "wrote nothing". The
journal's `files_touched_recorded` flag tells those two cases apart without
having to remember which tools are which.
`renforge_mod` lists the mod file and its manifest for `write`, every file
`remove` deleted (manifest included) as `files_deleted`, and the zip's absolute
path for `package`.
`renforge_saves` additionally labels the external savedir with `savedir`.
`renforge_saves(action="import")` lists each slot file it copied into the
session savedir (absolute paths), including on `SAVE_IMPORT_FAILED`, where it
names what was already written; a refused or failed import that wrote nothing
returns an empty list.
Because that directory is outside the project, its receipt path is absolute and
can contain the user's home directory; the journal is 0600 and in the project's state folder.

Each `<state>/activity.jsonl` entry carries `files_touched_recorded` and
`files_deleted_recorded` (true only when the result supplied a non-null
explicit list) plus `files_touched_truncated` (true when only a compact list is
stored). Entries keep at most 32 paths, and a path longer than 512 characters
counts as truncated. On truncation the full lists are spilled to
`files_touched_file` — a 0600 JSON sidecar under `<state>/activity-files/`
shaped `{"files_touched": [...], "files_deleted": [...]}` with the same path
base rules; `files_touched_file` itself is an absolute path, because the state
folder is outside the project. Only the newest 200 sidecars and at most 50 MiB are kept; older
ones are pruned after each write, the newest receipt is always kept, and files
that do not match the sidecar name pattern are never touched. If the sidecar
cannot be written safely, the compact entry is
still recorded and `files_touched_file` is omitted. On overflow, verbose params
and result details are dropped first, never the write receipt.

Recommended practices:

- prefer `renforge_game_state_compact` to the full state;
- bound `renforge_scan_project` with `file_glob`, `symbol`, `offset`, and
  `limit`;
- use a copy or branch before generating translations or distributions;
- list the UI before clicking and always pass `frame_id`;
- after a guard error, capture or list the current screen instead of replaying
  the same click.

## Environment variables

Set these next to the `renforge serve` command in the MCP client config.

| Variable | Default | Effect |
| --- | --- | --- |
| `RENFORGE_TOOLSETS` | all groups | Comma-separated tool groups the server lists (see [Toolsets](#toolsets)); `renforge_info` is always listed. Unknown names, or a value naming no group, are an error. |
| `RENFORGE_POLICY` | `off` | `enforce` requires `authorize=true` for destructive and open-world calls. Invalid values fail closed to `enforce`. See [POLICY.md](POLICY.md). |
| `RENFORGE_POLICY_ALLOW`, `RENFORGE_POLICY_ALLOW_RISK` | empty | Comma-separated operations or risk classes allowed without `authorize`; combined with the project policy file's lists. |
| `RENFORGE_ISOLATION` | `existing` | `temporary` (any case) isolates every launch's saves, persistent data, preferences, and HOME by default; any other value keeps the user's own, as before. `renforge_session` always isolates. |
| `RENFORGE_STATE_DIR` | `~/.local/state/renforge` (`%LOCALAPPDATA%\renforge` on Windows) | Where per-project state lives (see [Where RenForge keeps its files](#where-renforge-keeps-its-files)). |
| `RENFORGE_RUNTIME_DIR` | platform cache folder | Where running dashboards register themselves so MCP calls reach them. |
| `RENFORGE_STARTUP_TIMEOUT` | `180` | Seconds a launch waits for the game's bridge. |
| `RENFORGE_TRANSITIONS` | off | `on` keeps transitions in sessions with isolated preferences. |
| `RENFORGE_VSYNC` | off | `on` keeps vsync in launched games. |
| `RENFORGE_LANG` | `en` | Live Editor interface language: `en` or `zh-CN`. |
| `RENFORGE_ALLOW_PATHS` | unset | Live Editor write scope: glob patterns under `game/` that Save may write. See [LIVE_EDITOR.md](LIVE_EDITOR.md). |
| `RENPY_SDK_CACHE_DIR` | `~/.cache/renforge/sdks` | Where downloaded Ren'Py SDKs are kept. |
| `RENPY_SDK_STABLE_VERSION` | built-in | The version `version="stable"` resolves to. |
| `RENPY_SDK_BASE_URL`, `RENPY_SDK_ARCHIVE_URL` | renpy.org | A mirror for SDK downloads, or one archive URL to use as is. |

`RENFORGE_BRIDGE_*`, `RENFORGE_EDITOR_*` and `RENFORGE_SAVEDIR` are set by
RenForge for the launched game; do not set them yourself.

## Troubleshooting

| Symptom | Likely cause and resolution |
| --- | --- |
| `no running game` | If launch status is `starting`, wait and call `renforge_launch_status` again. Otherwise call `renforge_launch` or start the dashboard with `renforge ui`. |
| No `active_project` | Provide `project_path` explicitly (every tool accepts it), run the server from the game's directory, or select a project in the dashboard. |
| `expected_frame_id guard failed` | Refresh the observation/listing. For resized gameplay images, pass the complete `frame` descriptor rather than the delivered image hash. |
| A click lands at the wrong place under WSLg | Reuse the `coordinate_space` returned by visual search. |
| The MCP client cannot open a display | Start `renforge ui --project …`; launch requests are delegated to the display-owning process. |
| An MCP tool is missing | Update RenForge, then restart the MCP session to reload its catalogue. |
| Launch fails with `GAME_STARTUP_ERROR` | The game stopped on Ren'Py's error screen before the bridge started; `details.report` holds its `traceback.txt`. Players see the same error. A launch that failed while recompiling changed scripts is already retried once. |
| A ready launch carries `startup_warning` | The first attempt failed and a second one started: a recompile error that did not recur, or a shaped presplash that SDL's Wayland driver cannot open (the game then runs under X11, which needs an unlocked screen). When the second attempt fails too, its error keeps the first one in `details.first_attempt`. |
| Launch fails with `BRIDGE_CONNECTION_TIMEOUT` | `details.log_tail` shows where startup stopped. If it ends during window or GL setup (`Windowed mode.`, `Maximum texture size`), the window never opened: X11 windows do not open while the screen is locked, so unlock it or launch with `display='xvfb'`. |

## Quick verification

After configuration, ask the agent:

> Call `renforge_info`, inspect my Ren'Py project, then list the visible game
> controls.

A successful response contains the active project, a project summary, and
controls with canonical IDs, capabilities, bounds where established, and a source
`frame_id`. Unsupported custom input appears in `renforge_observe.input_candidates`.

## Scripted / non-interactive use

For scripts and CI, call tools through the FastMCP CLI instead of an
interactive MCP client. `--command` starts the stdio server, `--target` names
the tool, `--input-json` passes the arguments as a JSON object, and `--json`
prints FastMCP's result envelope.

The primary install is `uvx renforge@latest serve`, so use the same launcher as
your client config in place of `renforge serve`. If `renforge` is not on
`PATH`, substitute the matching launcher. The `fastmcp` CLI ships with the
`fastmcp` package, a RenForge dependency; install it if the command is not on
your `PATH`.

```bash
fastmcp call --command "renforge serve" --target renforge_info --json
```

`--json` prints FastMCP's envelope, not just the tool payload: `content` (the
result blocks), `is_error`, and `structured_content`. Read the tool payload from
`structured_content`, or from the first text block when it is absent; the
command exits 1 when `is_error` is true.

```bash
fastmcp call --command "renforge serve" --target renforge_inspect_project \
  --input-json '{"project_path": "/path/to/game"}' --json
```

Plain `key=value` tokens are not usable with `--command`: they bind to the
server-spec positional and the call is rejected, so pass arguments with
`--input-json`.

`renforge serve --project /path/to/game` only seeds the `active_project` that
`renforge_info` reports (a dashboard selection wins). It does not supply a
default project for tool calls: most project tools require `project_path`, so
pass it explicitly on every project-scoped call, as in the second example.

Under `RENFORGE_POLICY=enforce`, destructive and open-world calls are denied
unless the arguments include `"authorize": true` in `--input-json`, for example
`--input-json '{"project_path": "/path/to/game", "authorize": true}'`. See
[POLICY.md](POLICY.md). With `RENFORGE_POLICY=off` (the default) the flag is
unnecessary.

A long-lived MCP stdio client is the other documented route. Each
`fastmcp call` starts a fresh server process, so in-process state — including
launch ownership — is not shared between calls; use one stdio session for
multi-step launches that must stay under the same server's control. The
dashboard's `/api/*` endpoints are a UI surface, not a documented scripting API.

`renforge.server.create_app()` is used by the test suite to build the server
in-process, but it is not a declared stable public API and no module-level app
instance is provided; scripts should use the stdio/CLI route.

> **Version note:** These flags were checked against FastMCP 4.0.5.
> `pyproject.toml` only requires `fastmcp>=2.0.0`, so CLI flags may differ on
> other releases; check `fastmcp call --help`.
