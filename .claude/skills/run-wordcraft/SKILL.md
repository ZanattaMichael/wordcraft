---
name: run-wordcraft
description: Build, run, drive and screenshot WordCraft (the Rust/egui word processor) on headless Linux. Use when asked to start or launch WordCraft, click or type in its UI, take a screenshot, open a dialog or ribbon tab, render a page, convert or inspect a document with wordcraft-cli, run its MCP server, or run its tests (cargo xtask ci).
---

WordCraft's desktop app (egui + wgpu) is driven through its JSON-lines control port by
`.claude/skills/run-wordcraft/driver.py`: `start` launches `target/release/wordcraft` on its own
Xvfb display with `--control 7981`, and every other subcommand sends one control request
(`docs/control-protocol.md`) and prints the JSON reply. No window manager, no GPU needed.

All paths are relative to the repo root.

## Prerequisites

Ubuntu 24.04 container, Rust from rustup (cargo 1.97 used; workspace needs ≥ 1.90).

```bash
apt-get update
apt-get install -y --no-install-recommends xvfb xdotool libxkbcommon-x11-0 libegl1 libegl-mesa0 mesa-vulkan-drivers
```

- `libxkbcommon-x11-0`: winit dlopens it on X11 (panic without it).
- `libegl1 libegl-mesa0` (GL via llvmpipe) **or** `mesa-vulkan-drivers` (lavapipe): either one
  is enough for wgpu; install both.
- `xdotool`: gives the window keyboard focus (see Gotchas). ImageMagick (`import`) is optional,
  only for `driver.py xshot`.

## Build

```bash
cargo build --release --locked -p wordcraft -p wordcraft-cli
```

About 7 minutes cold on 4 cores; no `-dev` packages needed. (`driver.py start --build` runs the
`wordcraft` half of this for you.)

## Run (agent path): the driver

```bash
D=.claude/skills/run-wordcraft/driver.py
python3 $D start --sample          # -> ready: pid …, port 7981, DISPLAY :99, window 1440x920 pt, keyboard focus yes, 0.4s
python3 $D shot 01-sample          # -> /tmp/wordcraft-shots/01-sample.png (1440x920, 1 px = 1 pt)
python3 $D stop
```

A real flow that was run end to end (select → Ctrl+B → type → dialog → ribbon → save):

```bash
python3 $D start --sample
python3 $D cmd select.text '{"text":"Every Thursday evening"}'
python3 $D key B --cmd                                   # real shortcut through egui
python3 $D cmd select.text '{"text":"old print shop"}'
python3 $D type "community print shop"                   # replaces the selection
python3 $D cmd ui.dialog '{"name":"font"}'
python3 $D shot 03-font-dialog
python3 $D click 809 576                                 # the dialog's OK button
python3 $D cmd ui.tab '{"tab":"Insert"}'
python3 $D inspect | python3 -c "import json,sys; r=json.load(sys.stdin)['result']; print(r['dialog'], r['ui']['tab'], r['dirty'])"
python3 $D render page1 --page 1                         # engine-rendered page PNG
python3 $D cmd file.save '{"path":"/tmp/wordcraft-shots/edited.docx"}'
./target/release/wordcraft-cli text /tmp/wordcraft-shots/edited.docx | head -3
python3 $D stop
```

| command | what it does |
|---|---|
| `start [--sample] [--file F] [--port N] [--build]` | Xvfb `:99` + app, waits for the first frame, focuses the window |
| `shot [NAME]` | window PNG via the app's own `ui.screenshot` → `/tmp/wordcraft-shots/NAME.png` |
| `xshot [NAME]` | X-server grab of the whole 1920x1200 Xvfb screen (needs ImageMagick) |
| `render [NAME] [--page N] [--scale S]` | one page rendered by the engine (no UI chrome) |
| `cmd ID [JSON]` | `engine.execute` any command id, e.g. `format.bold`, `ui.tab {"tab":"Review"}` |
| `call METHOD [JSON]` | any raw control method (`ui.clickText`, `ui.drag`, `ui.resize`…) |
| `type TEXT` / `key KEY [--cmd --shift --alt]` / `click X Y [--right] [--count N]` | real egui input |
| `inspect [ui\|doc]` | `ui.inspect` (tab, dialog, dirty, caret, page rects) or `document.inspect` (blocks, runs) |
| `commands [FILTER]` | engine command ids with enabled state and ribbon location |
| `script FILE.jsonl` | one `{"method":…,"params":…}` per line, stops at the first `"ok": false` |
| `focus` / `status` / `log` / `stop` | re-focus window / pid+port / app stderr / quit app and Xvfb |

Every subcommand exits 1 when the reply has `"ok": false`. Screenshots and renders land in
`/tmp/wordcraft-shots/` (`WORDCRAFT_SHOT_DIR`); pid state and `app.log` in `/tmp/wordcraft-run/`.
Window coordinates for `click` are points in the 1440x920 window; the `shot` PNG maps 1:1, so
read coordinates straight off it (ribbon tabs sit at y≈53). Prefer commands over clicks when
one exists.

## Screenshot without any display: `ui_shot`

The repo's offscreen harness renders the whole window through wgpu with no X server (2x scale).

```bash
cargo build --release --locked -p wordcraft-ui-egui --example ui_shot      # ~7 min cold
mkdir -p /tmp/wc-uishot
cat > /tmp/wc-uishot/script.jsonl <<'EOF'
{"method": "engine.execute", "params": {"command": "select.text", "params": {"text": "Every Thursday evening"}}}
{"method": "engine.execute", "params": {"command": "format.bold"}}
{"method": "ui.tab", "params": {"tab": "Layout"}}
{"shot": "/tmp/wc-uishot/layout.png"}
EOF
env -u DISPLAY ./target/release/examples/ui_shot /tmp/wc-uishot/script.jsonl   # -> 2880x1800 PNG in <1 s
```

Opens the sample document unless the first line is `{"empty": true}`. Good for one-shot
before/after pictures; use the driver when you need state across many steps.

## Direct invocation (no window): `wordcraft-cli`

Most engine, format and layout changes can be checked here without the GUI.

```bash
C=./target/release/wordcraft-cli
mkdir -p /tmp/wc-cli
$C run --template sample --cmd 'select.text={"text":"Studio"}' --cmd format.bold --save /tmp/wc-cli/out.docx
$C info /tmp/wc-cli/out.docx
$C render /tmp/wc-cli/out.docx /tmp/wc-cli/p1.png --page 1 --scale 1
$C convert /tmp/wc-cli/out.docx /tmp/wc-cli/out.pdf
```

MCP server, headless and bridged to the app the driver started:

```bash
# the --connect half needs `python3 .claude/skills/run-wordcraft/driver.py start` first
C=./target/release/wordcraft-cli
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_text","arguments":{}}}' | $C mcp
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"screenshot","arguments":{}}}' | $C mcp --connect 127.0.0.1:7981
```

## Run (human path)

```bash
xvfb-run -a cargo run --release -p wordcraft -- --sample --control 7981   # window on a hidden display; Ctrl-C to stop
```

Only useful with a real display (drop `xvfb-run`). The web build (`apps/wordcraft-web`, trunk)
exists but was not run here.

## Test

```bash
rustup target add wasm32-unknown-unknown   # the gate's last step builds the web app
cargo xtask ci                             # fmt, clippy -D warnings, tests, assets, layers, wasm
```

Expect `CI summary: all 6 steps passed (fmt, clippy, test, assets, layers, wasm)`: 250 tests, about
19 minutes cold on 4 cores. The `wordcraft-ui-egui` tests render with egui_kittest's wgpu backend;
they passed on the same software drivers listed in Prerequisites. For a quick loop, `cargo test -p <crate>`.

## Gotchas

- **Typing does nothing until the window has X focus.** Xvfb has no window manager, so the window
  never gets keyboard focus and egui silently drops every `ui.key`/`ui.text` event, while
  `ui.click` still works. Symptom: replies say `"ok": true` but `inspect` shows `focused: false,
  dirty: false`. `start` fixes it with `xdotool windowfocus`; run `driver.py focus` if it was lost.
- **`"ok": true` from `type`/`key`/`click` means queued, not applied.** Events run on the next
  frame; read back with `inspect` or `cmd document.text` before asserting.
- **AutoSave rewrites the file you opened.** It is on by default for any document with a path:
  after `start --file X` or `cmd file.save {"path":…}`, every later edit is written back to that
  file within about 3 seconds. Always open a copy.
- **`ui.tab` takes `{"tab": …}`, `ui.dialog` takes `{"name": …}`.** A wrong key is not an error:
  `ui.tab {"name":"Insert"}` silently switches to Home.
- **`commands` lists engine ids only.** UI ids (`ui.tab`, `ui.dialog`, `ui.backstage`,
  `ui.closeDialog`, `ui.dark`, `ui.collapseRibbon`, `ui.openFileDialog`) live in
  `crates/ui-egui/src/lib.rs`; dialog names in `Dialog::open` in `crates/ui-egui/src/dialogs.rs`.
- **The window title is the document title**, not "WordCraft", so `xdotool search --name
  WordCraft` finds nothing; the driver searches by pid.
- The driver sets `WORDCRAFT_NO_PREFS=1` so runs don't read or write `~/.config/wordcraft/ui.json`.
- `libEGL warning: DRI3 error: Could not get DRI3 device` in `app.log` is harmless under Xvfb.
- The control port has no authentication: anything on the machine can drive the app while it runs.

## Troubleshooting

- **`panicked at …xkbcommon-dl…: Library libxkbcommon-x11.so could not be loaded`** on `start`:
  `apt-get install -y libxkbcommon-x11-0`.
- **`Error: Wgpu(CreateSurfaceError(… FailedToCreateSurfaceForAnyBackend({}) …))`** on `start`, or
  **`Failed to create render state: CustomNativeAdapterSelectionError("No adapter found")`** from
  `ui_shot`: no software GPU driver. Install `libegl1 libegl-mesa0` or `mesa-vulkan-drivers`.
  (Reproduced by hiding both with `VK_ICD_FILENAMES` and `__EGL_VENDOR_LIBRARY_FILENAMES`.)
- **`driver: can't reach the app on 127.0.0.1:7981`**: not started or it crashed; `driver.py log`.
- **`start` says `keyboard focus NO (install xdotool)`**: `apt-get install -y xdotool`, then
  `driver.py focus`.
