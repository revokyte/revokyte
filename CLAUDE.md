# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## About

An embedded sim racing instrument cluster built with Python/pygame. Runs on Raspberry Pi 4/5 with a supported DSI touch display (Raspberry Pi Touch Display 2 at 720×1280, or the Waveshare 5″ DSI at 800×480 — see the profiles in `peripherals/display.py`). Renders a real-time dashboard at 60 fps from telemetry delivered as NDJSON to `udp://127.0.0.1:5600`.

The cluster is **game-agnostic**: it only ever reads that localhost NDJSON. Each supported game is a separately-released **feed program** the user installs, which reads the game's telemetry and re-emits the `TelemetryFrame` schema. GT7 uses the [granturismo](https://github.com/chrshdl/granturismo) proxy (decrypts GT7's Salsa20 UDP stream); Assetto Corsa Competizione uses a feed over its native UDP Broadcasting API. The feeds are listed as data in `addons/feeds.py` (`FeedDescriptor`s); the install flow, settings UI, and IP-entry are generic over a descriptor — there is no per-game branching in the runtime. A feed may set two optional `TelemetryFrame` fields, `native_delta_ms` and `track_name`, which `DeltaSignal`/`TrackSignal` republish instead of computing (ACC provides both; GT7 leaves them unset and the GT7 compute paths run).

## Commands

```bash
# Install deps and activate venv
uv venv && source .venv/bin/activate && uv sync

# Run the app (defaults to demo mode — no PS5 required)
python -m instrument_cluster

# Run tests
uv run pytest

# Run a single test file
uv run pytest tests/instrument_cluster/core/plugin_system/test_plugin_manager.py

# Run tests with coverage
uv run pytest --cov=instrument_cluster
```

**Config location**: `~/.config/instrument-cluster/config.json` (or override with `IC_CONFIG_PATH` env var).

## Deployment to the Pi

The Pi (`root@instrument-cluster.local`) runs the package as bytecode-only (`.pyc`, no source). **Always ssh/scp as `root@instrument-cluster.local`** — never a numeric IP. The DHCP address drifts on its own (observed .79 → .81 → .83 within a week), not just on image flashes, so any remembered IP is assumed stale. (A re-flash also regenerates the SSH host key; remove the stale entry with `ssh-keygen -R instrument-cluster.local` when ssh complains.) To push a change:

```bash
# 1. Compile the changed module(s) — strip the cpython-3XX infix so the
#    filename matches what's already on the Pi.
python3 -c "import py_compile; py_compile.compile('src/instrument_cluster/signals/delta_signal.py', cfile='/tmp/delta_signal.pyc', doraise=True)"

# 2. Make the read-only rootfs writable, then copy to the Pi (mirror the
#    package subdirectory structure)
ssh root@instrument-cluster.local "mount -o remount,rw /"
scp /tmp/delta_signal.pyc root@instrument-cluster.local:/usr/lib/python3.12/site-packages/instrument_cluster/signals/delta_signal.pyc

# 3. Restore read-only and restart. The remount,ro must happen while the
#    service is stopped — a running cluster pins the rootfs writable via a
#    shared-writable mmap of mesa's GPU shader cache.
ssh root@instrument-cluster.local "systemctl stop instrument-cluster; sync; mount -o remount,ro /; systemctl start instrument-cluster; sleep 2; systemctl is-active instrument-cluster"
```

The install root is `/usr/lib/python3.12/site-packages/instrument_cluster/`. Subdirectories (`signals/`, `ui/`, etc.) mirror `src/instrument_cluster/`. The local Python used to compile must be 3.12 to match the Pi's interpreter.

## Architecture

### Data Flow

The system is a **blackboard**: a 60 fps main loop (`main.py`) drives producers that write to `VehicleBus` and consumers that read from it. All signal processors run directly from the main loop — `SignalPipeline` (which bundles `TelemetrySource`, `TrackSignal`, and `DeltaSignal`) alongside the health monitor and any extension-registered signal processors (see Extensions below; none on the community image) — so the data-acquisition layer is never paused by UI state changes. `DashboardState` is a pure consumer: it only updates the view and peripherals. `VehicleBus.tick()` only updates `health`.

```mermaid
flowchart LR
    GT7["Gran Turismo 7<br/>(PS5)"]
    PROXY["granturismo<br/>proxy.py"]

    subgraph producers["Producers — write to the bus"]
        direction TB
        subgraph pipeline["SignalPipeline (main loop)"]
            SRC["TelemetrySource<br/>UDP · demo readers"]
            TRACK["TrackSignal<br/>track_id · track_name"]
            DELTA["DeltaSignal<br/>delta_diff · _stable"]
        end
        PRO["Extension processors<br/>(none installed = none)"]
        HEALTH["SystemHealthMonitor<br/>watchdog · backlight"]
    end

    BUS{{"VehicleBus<br/>━━━━━<br/>frame · signals<br/>app_state · health"}}

    subgraph consumers["Consumers — read the bus"]
        direction TB
        VIEW["DashboardView<br/>gauges + widgets"]
        PLUGINS["Plugins<br/>(PluginBusView)"]
        PERIPH["ShiftLights<br/>(plugin)"]
    end

    GT7 -->|"Salsa20 · UDP"| PROXY
    PROXY -->|"NDJSON · UDP"| SRC
    SRC -->|"TelemetryFrame"| BUS
    TRACK --> BUS
    DELTA --> BUS
    PRO -.-> BUS
    HEALTH --> BUS

    BUS --> VIEW
    BUS --> PLUGINS
    BUS --> PERIPH

    VIEW --> DISP["Display<br/>native logical → panel"]
    PLUGINS -.->|"sprites"| VIEW
    PERIPH -->|"8-LED bar"| LEDS["Blinkt! shift lights"]
```

**`VehicleBus`** (`core/vehicle/vehicle_bus.py`) is the central shared data structure passed to everything:
- `.frame` — raw `TelemetryFrame` from GT7 (speed, RPM, gear, tire temps, position, flags, etc.)
- `.signals` — computed dict values from signal processors (e.g. `delta_diff`, `track_id`, `track_name`)
- `.app_state` — UI-level state (e.g. `delta_color`)
- `.health` — `SystemHealthMonitor` for systemd watchdog / backlight control

### Signal Processors

Signals in `signals/` enrich telemetry before the UI sees it. Each returns a dict that is merged into `VehicleBus.signals`. All signal processors are driven directly by the main loop — never by a UI state — so the data-acquisition layer continues uninterrupted even when settings menus are open.

- **`LinkSignal`** — telemetry link supervision. Readers hold their last frame forever (there is no TTL on `UdpJsonlReader._latest`), so this is what stops a sleeping console / crashed feed / dropped Wi-Fi from leaving frozen values on screen looking live. Publishes `telemetry_stale` + `telemetry_age_s`, measured off `received_time`, which the **reader** stamps on arrival (feeds must not — the schema's 0.0 default previously let a conforming feed silently kill the delta and fuel signals forever). Threshold 1 s, relaxed to 10 s while `paused`/`loading_or_processing`. Runs *before* the pipeline's no-frame guard so an inert reader is reported rather than shown as a blank dash. `NoSignalWindow` raises a full-width NO SIGNAL banner on it (gauges keep their last values at full brightness — dimming was tried and dropped as it only hurt legibility).
- **`SignalPipeline`** — main-loop owner of `TelemetrySource`, `TrackSignal`, `DeltaSignal`, `FuelSignal`, and `LinkSignal`. Its `update(bus, dt)` is called every frame before `state_manager.update()`. `start()` / `stop()` are called by `DashboardState.enter()` / `exit()`; an `_active` guard makes it a no-op when the dashboard is not in the stack. `sync_mode()` handles telemetry-mode switches on dashboard resume.
- **`DeltaSignal`** — wraps a Cython/fallback delta calculator. Publishes `delta_diff` (raw, 60 Hz) and `delta_diff_stable` (sample-and-hold with 200 ms refresh + 20 ms hysteresis for display). Resets on lap change and track change. Also publishes `delta_state` — why there is *no* delta, in GT3-dash vocabulary the widget shows in place of the number: `BEACON` (not in a timed lap), `REF_LAP` (recording the first reference), `NO_REF` (an established reference was discarded — distinct from never having had one). `None` means armed. The widget must never hold the previous number when the signal goes None: a delta from an earlier lap, reference or track is indistinguishable from a live one. Note the delta is legitimately unavailable for the first ~3 laps of a session (lap 1's reference is discarded by `_skip_first_reference`, lap 2 builds it, lap 3 uses it) — the state words are what make that wait legible instead of looking broken. Changing the diff reference mode in settings takes effect mid-lap: the calculator (`delta-calculator` ≥ 0.2.0) keeps previous-lap and fastest-lap references side by side and swaps the active one, and `DeltaSignal` republishes `delta_ref_lap_time` and force-refreshes the stable display on the switch.
- **`TrackSignal`** — identifies the current track from `db/tracks.json` and never revises a published name: it shows `---` until the evidence is conclusive. A track is recognized when the car's path exactly crosses a start/finish gate segment (direction-matched) *and* that crossing coincides with a `lap_count` tick — GT7 increments the counter exactly at the true line, which is what rejects mid-lap crossings of other layouts' lines (a Nürburgring 24h lap drives across the Nordschleife and Tourist layouts' gates). Gate-sharing siblings (Brands Hatch GP/Indy, Suzuka/East, …) stay unpublished while the lap decides: candidates are eliminated when the car escapes their bounding box, and a full line-to-line lap is fitted against the survivors' boxes. Six pairs have near-identical boxes and are geometrically unresolvable, picked by best fit: Spa / Spa 24h Layout, Le Mans / No Chicane, Daytona Tri-Oval / Road Course, Fuji / Fuji Short, Monza / No Chicane, and Barcelona-Catalunya GP / No Chicane. Publishes `track_id` and `track_name`. Locks once identified; a loading/processing blip (track/replay load) clears the lock so a newly loaded circuit is re-identified from scratch.
### State Machine

`StateManager` maintains a **stack** of `State` objects. Pushing a state pauses (but doesn't exit) the one below; popping resumes it. **Input goes to the current state only** — a covered state's widgets must never hit-test taps. (Events used to fall through the stack until some state returned True, and views return False for raw touches: on the 7″ panel the Wi-Fi keyboard ghost-pressed the paused Setup screen's rows underneath — a tap on OK could push a second `WifiSetupState` mid-connect, and typing a password flipped the status-lights toggle.) The `draw()` path uses dirty-rect rendering — only changed regions are flushed to display each frame.

### Window Layering

The main loop drives a `WindowManager` compositor (`ui/window_layering.py`), not the StateManager directly. The StateManager is the **BASE** layer; `OverlayWindow`s (z-ordered `WindowLayer`: NOTIFICATION, SYSTEM_ALERT topmost) are composited after it every frame — re-blitted whenever a base dirty rect touches them — so the base keeps running live underneath and no widget can ever draw over an overlay (sprite groups can't guarantee that; separate `LayeredDirty` groups only z-order sprites that repaint in the same frame). Overlay windows get events before the base and their disappearance triggers a base repaint. The community build registers three overlay windows itself, all in `main.py`: `NoSignalWindow` (`ui/no_signal_window.py`) on SYSTEM_ALERT, `WifiStatusWindow` (`ui/wifi_status_window.py`) on NOTIFICATION — the "Connecting to Wi-Fi" pill on credentials-provisioned boots (see the Wi-Fi first-boot gate below), and `FeedUpdateWindow` (`ui/feed_update_window.py`) on NOTIFICATION — the stale-feed notice, shown once per boot and dismissed with a tap (it dims the live view 35% via `ui/widgets/base/modal_dimming.py::ModalDimming`, which states dim strength as a percentage rather than the raw alpha `ModalBackdrop` takes). `NoSignalWindow` is on SYSTEM_ALERT because a dead link — telemetry link loss must never be drawn over, which is precisely what that layer guarantees, and the compositor also repaints the base when it disappears. Extensions add their own notification popups via the extension runtime. All are gated on a duck-typed opt-in the active state sets: `allows_notification_popup` for popups, `allows_system_alert` for the alert and the Wi-Fi pill (all `DashboardState`-only, so Setup — where a dead feed is configured — is never covered).

Layers settle *pixels*; whether two windows may be up at once is arbitrated separately by `WindowManager._arbitrate()`, modelled on AAOS's `OverlayViewGlobalStateController`: the topmost visible window owns the policy, and a window it occludes is **withdrawn** — not composited, no events — rather than merely covered. Two class-attr opt-ins express it, both default off: `occludes_below` (suppress everything under me) and `show_when_occluded` (I survive someone else's occlusion). Only the *topmost* window is asked whether it occludes, so a NOTIFICATION can never suppress a SYSTEM_ALERT. Arbitration is stateless and recomputed every frame, so withdrawal is a deferral and never a dismissal — the window returns by itself when the occluder goes, and the compositor repaints the base on withdrawal exactly as it does when a window hides itself. A window therefore has two notions of being up: `visible` (its own request) and `showing` (what survived arbitration); anything that must match the pixels — compositing, event routing, dirty-sprite rising edges — keys off `showing`. `NoSignalWindow` declares `occludes_below`: a dead link is read alone, and it otherwise sliced across `FeedUpdateWindow`'s card while that card's 35% dimming knocked back the very gauges the banner marks as stale. The remedy stays reachable — the banner clears the footer, so the Setup button and its "Telemetry (update)" row are still there. `tools/preview_window_arbitration.py` walks both transitions on the panel and can toggle arbitration off to show the collision it fixes.

Key states in `states/`:
- `DashboardState` — the main racing view; a pure consumer that links plugin sprites into `DashboardView.plugin_layer` and re-links whenever `PluginManager.generation` changes (plugin reload). The view itself owns only chrome (Setup button, bezel LED strips). Signal processing is handled by `SignalPipeline` in the main loop, not here.
- `SetupState` — first-run / connection setup; renders extra rows contributed by extensions (none installed = none shown)
- `EnterIPState` — PS5 IP entry via soft keyboard
- `AccelTestState` — Testing & Validation: the standing-start acceleration timer (see below)

### Wi-Fi first-boot gate

The dashboard is never gated on connectivity. With credentials provisioned the boot goes straight to the gauges while association completes in the background — `WifiStatusWindow` shows the "Connecting to Wi-Fi" pill (association polled on a daemon thread; boot status only, it never returns once associated — link loss mid-session is telemetry loss and belongs to NO SIGNAL). Only a true first boot pushes `WifiSetupState` (on-screen scan + join): `main.run` gates on `WifiManager.has_credentials()`, which must read two *well-formed* configs as unprovisioned — the header-only seed written by the OS image's prepare-data-dirs.service, and the flashable image's boot-partition template (`board/*/wpa_supplicant-wlan0.conf` in instrument-cluster-os) whose placeholder `network={` block (`ssid="YOUR_WIFI_SSID"`) wifi-setup.service copies onto /data even when the user never edited it. Treating that unedited template as credentials skips setup and leaves the boot polling a network that cannot exist. Both repos guard on the literal `YOUR_WIFI_SSID`: the OS install script refuses to install an unedited template, but devices flashed before that fix already carry it on /data, so the app-side check in `has_credentials()` is what heals them. On dev machines (no wlan0) neither branch triggers.

### Extensions (`extensions.py`)

**The base build is complete and fully offline** — no backend traffic of any kind; every widget is free. At startup `main.py` calls `extensions.runtime.load(...)`, which discovers installed add-on distributions via the **`instrument_cluster.extensions` entry-point group** and calls each registered `wire(runtime)` hook. No installed extensions is the silent default; a broken one logs, has its registrations rolled back, and the app degrades to the plain cluster. The community source never names any particular extension — proprietary add-ons ship as their own distributions (in their own OS image) declaring `my_extension = "my_package:wire"` like anything else. Inside `wire(runtime)` an extension registers:

- **signal processors** (`runtime.add_signal_processor`) — `update() -> dict` polled every frame into `bus.signals`, `stop()` at shutdown;
- **Setup rows** (`runtime.add_setup_entry(SetupEntry(...))`) — the entry's `make_state(state_manager)` builds the target state, its pygame event types are allocated once at registration, and `button_text` may be a callable (re-evaluated per Setup entry, so the label can track extension state);
- **overlay windows** — straight onto `runtime.window_manager`;
- **plugin classes** — appended to `plugin_manager.contributed_classes`, ranked between packaged and external (an external file can still shadow one by `plugin_id` for a hot fix);
- a **feature provider** on `runtime.plugin_manager` (`has_feature`/`invalidate`, replacing the default `NullFeatureProvider` that grants nothing) — `load()` runs before `load_plugins()` exactly so this swap can happen first.

`ExtensionRuntime` is a module-level singleton because SetupView is rebuilt on every entry and only reads the registered entries. The entry-point group, the `wire` signature, and these registration points are the whole contract.

### Plugin System

All telemetry gauges are plugins; `DashboardView` keeps only system chrome. `PluginManager` (`core/plugin_system/plugin_manager.py`) loads from two directories:

- **packaged** `src/instrument_cluster/plugins/` — every gauge shipped in the image, all free (`gear`, `speed`, `rpm`, `tire_temps`, lap/delta widgets, `track_name`, `fuel_strategy`, `shift_lights`), imported as package modules. The default layout shows the fuel pair in the third left slot; a current-lap-time block exists only as a custom-dashboard building block in the registry;
- **external** `/data/plugins/<slug>/<slug>.py` (`~/.instrument-cluster/plugins` on dev machines) — a writable override directory, compiled from source at load. An external `plugin_id` shadows a packaged one, which allows dropping in a widget fix without an OS update.

A plugin extends `GenericPlugin` (or `WidgetPlugin` to wrap `ui/widgets` gauges) from `core/plugin_system/sdk.py` and declares class-attr metadata: `plugin_id`, `version`, `required_feature` (skipped unless the manager's `feature_provider` grants it — the default grants nothing, and no community plugin declares one; an extension may install its own provider from its wire hook), `excluded_by_feature` (yields its slot when the excluding plugin is present), `dashboard_only` (only updates while the dashboard is the active state, e.g. the Blinkt LED bar), `exclusive` + `provider_ready()` (exclusive dashboard provider — see below). Layout comes from `core/plugin_system/plugin_layout.py::LayoutContext` (status-lights shifts). Plugins read data via `self.get_signal(key)` (frame attrs → `bus.signals` → `bus.app_state`) through a read-only `PluginBusView`. Reloads are thread-safe (`request_reload`, executed on the main loop).

### Generic machinery for exclusive dashboard providers

Extension-contributed plugins load through the same `PluginManager`; the hooks below are feature-agnostic:

- **Exclusive provider** (`sdk.py`): a plugin class with `exclusive = True` whose `provider_ready()` returns True takes over the screen — every other `WidgetPlugin` is suppressed while hardware plugins (shift-lights) keep running. Not ready / feature not granted / raising falls back to the standard gauges.
- **Paging protocol** (duck-typed by `DashboardState`): `pages() / active_page() / set_active_page(i)` on the provider instance drive the page dots, footer label, and swipe. `config.dashboard_slot` stays as generic page state the provider persists.
- **Background sync hook** (`background_sync(SyncContext) -> SyncOutcome` classmethod): `PluginManager.sync_hooks` collects it from every feature-granted candidate class — independent of readiness/instantiation. Community builds collect an empty list and never dispatch one; an extension's sync loop is the dispatcher.

The widget registry (`ui/widgets/registry.py`) stays here (free widgets, shared surface) and — with the SDK, config, and logger modules — is the informal API contract external plugins import (see the `GenericPlugin` docstring). The external plugin directory (`/data/plugins`, dev `~/.instrument-cluster/plugins`) remains a generic override mechanism: an external `plugin_id` shadows a packaged one, allowing a widget fix without an OS update.

### Rendering & Skins

Every panel renders at **native resolution** with a **dedicated, hand-tunable skin** — one geometry set per resolution in `ui/skins/` (`skin_1280x720.py` is the original design verbatim, `skin_1024x600.py` and `skin_800x480.py` were seeded by `tools/gen_skin_seed.py` and are hand-tuned in place). A `Skin` (`ui/skins/schema.py`) holds every layout number in native integer pixels, grouped by area (`dashboard`, `style`, `header`, `setup`, `keyboard`, `numpad`, `overlays`); `active_skin()` resolves by the active profile's `logical_size`, lazily at construction time — **never at import time** (views import before `Display()` exists; the lazy dev fallback is SKIN_1280). Skins may differ structurally, not just in size: the 800×480 Setup grid packs its 5 rows into a 103 px shorter viewport, so its cells are 82 px to the 1280's 124. Schema fields carry axis metadata (`x`/`y`/`u`/`font`/`font_pixel`/`const`) so the seed generator can scale mechanically; Pixeltype sizes snap to multiples of 8 (`test_pixel_fonts_snapped` enforces it — that font renders ragged off its grid).

The old design-space helpers (`sx/sy/su/srect/spos` in `ui/utils.py`, `DESIGN_WIDTH/HEIGHT`) survive for exactly two things: the **custom-dashboard spec space** (user layouts are authored in 1280×720 on every panel; `registry.py` composes `scale_uniform()` into `font_scale`, and the external browser-builder stays 1280×720 — do not "fix" it to per-skin) and **self-contained decoration** (small paddings/glyphs inside a widget). Skinned code passes native pixels and `load_font_px`; never mix a `su()`-scaled value arithmetically with a skin value. Per-skin layout tests live in `tests/instrument_cluster/ui/test_skins.py` (bounds, overlap, pixel-font grid, per-profile resolution via the `force_profile` fixture in `tests/conftest.py`). Preview any skin on a dev machine: `python tools/preview_dashboard.py --display waveshare_5 --shot /tmp/dash.png` (headless PNG), or set `"display": "waveshare_5"` in the config for a live native-size window; every `tools/preview_*.py` accepts `--display`.

**The skin editor** (`python tools/skin_editor`) is the designer-facing way to tune all of this: an EB-GUIDE-style standalone pygame app (view tree left, WYSIWYG canvas center — rendered by the *real* view/widget code — properties right) that edits geometry/fonts per skin with drag/resize/nudge on the canvas, the shared color palette (live overrides via `Color.rgb()`/`set_palette_override`; color defaults resolve at construction, never in signatures), and the icon registry (`ui/icons.py`, all material-symbols glyphs by semantic name, `Icon.X.glyph()`; a picker browses the TTF). Saving rewrites `skin_*.py` through `ui/skins/serialize.py::emit_skin_module` (`scale=None` = verbatim; the seed generator shares this code path) and surgically rewrites the member lines of `colors.py`/`icons.py` (golden tests in `tests/tools/` pin byte-identical no-op saves). The editor isolates its config (temp `IC_CONFIG_PATH`) and uses `set_skin_override`/`set_icon_override` — tooling-only hooks the app never calls.

- On the Raspberry Pi Display 2: uses `HardwareRenderer` (OpenGL) which rotates the surface 270° to account for the portrait display mounted in landscape orientation (1:1 pixels, no resampling).
- On the Waveshare panels (7″ 1024×600, 5″ 800×480): software renderer at native panel resolution (`logical_size == physical_size`, no post-scale). The profile is auto-detected by panel resolution in `peripherals/display.py`.
- On other platforms (dev): standard `pygame.display.update()` with dirty rects.
- Tests run with `SDL_VIDEODRIVER=dummy` (set in `tests/conftest.py`).

### Telemetry Modes

`TelemetryMode.DEMO` is purely synthetic (`telemetry/demo.py`), not a recording: `DemoReader` computes each frame from the time since it was created (speed and rpm sweep as sines, the gear cycles P, N, R, 1..5 every 5 s, tyre temps from a time-seeded `random.Random`), and `SignalPipeline` swaps in `DemoDeltaSignal`/`DemoFuelSignal` for the real delta and fuel processors. `TelemetryMode.UDP` listens on `udp_host:udp_port` for live telemetry from whichever feed is installed — on the appliance the runtime mode is UDP for *every* game, so there is no per-game `TelemetryMode`. The settings dropdown offers Demo plus one entry per `FeedDescriptor` in `addons/feeds.py`; on the Pi, selecting a feed routes through `EnterIPState` → `InstallState` (which installs that feed's signed tarball and writes its env file). The installer resolves the descriptor's **pinned** `version` tag, never GitHub's latest release — the feed and the cluster share the `TelemetryFrame` schema, so a feed published after the image was built could speak a shape it doesn't understand. The pin must match the git ref in pyproject's `pc` extra (which is what desktop DIRECT mode reads in-process); `tests/instrument_cluster/addons/test_feeds.py` fails if they drift. Bumping a feed is therefore an image change: descriptor + pyproject + release. Because the install lives on `/data` and survives OS updates, the pin alone says nothing about what a device is *running* — `feeds.feed_needs_reinstall` compares `config.telemetry_feed_version` (written at install) against the pin, and the mismatch is surfaced three ways: a startup log warning, the Setup row reading "Telemetry (update)", and `FeedUpdateWindow`. An empty recorded version counts as stale, so devices installed before the field existed converge on one redundant re-install, then sets `telemetry_mode=udp` and records the chosen feed id in `config.telemetry_feed` (an opaque key used only to show the current dropdown selection). Switch via `ConfigManager.set_telemetry_mode()` / `set_telemetry_feed()` or through the settings UI.

`TelemetryMode.DIRECT` is the desktop path: no proxy is installed; the selected feed is read **in-process** by the reader its `FeedDescriptor.direct_reader` factory builds (`Gt7DirectReader` in `telemetry/gt7_direct.py` runs granturismo's `Feed` on a background thread and maps `Packet` → `TelemetryFrame`; `AccDirectReader` in `telemetry/acc_direct.py` does the same over acc-telemetry's Broadcasting `Feed` — its `Frame` dataclass deliberately uses the schema's field names, so the mapping is `dataclasses.asdict` + pydantic validation). Off the appliance (`not is_raspberry_pi()`), the settings dropdown only offers Demo plus direct-capable feeds, and `EnterIPState`'s OK skips `InstallState`: it sets `telemetry_mode=direct`, `telemetry_feed`, and `config.direct_host` (the console IP), then pops to the dashboard, whose resume applies the mode via `SignalPipeline.sync_mode()` (which also rebuilds the reader when only `direct_host` changed). `granturismo` and `acc-telemetry` are **optional dependencies** (the `pc` extra, pinned as git references — the PyPI name `granturismo` is an unrelated project); a build without them, or a failed reader construction, degrades to an inert reader (no frames, error logged) — never to demo motion. DIRECT uses the real (non-demo) delta/fuel signal processors.

### Delta Calculator

The delta calculator is **vendored pure-Python source** in `core/delta_calculator/` (copied from `../delta-calculator`, upstream version in its `__init__.__version__`; its tests are vendored too as `tests/instrument_cluster/core/delta_calculator/test_delta_calculator.py` / `test_delta_math_lite.py`). It ships inside the OS bundle and is bytecode-compiled with the image's own interpreter — the previously separate compiled package (`delta_calculator` `.so`) was pinned to one CPython ABI and would break the moment an OS update bumps Python; a stale copy in site-packages on deployed devices is simply ignored. `delta_calc.py::make_delta_calculator()` returns the vendored implementation, with the no-op `delta_calc_fallback.py` as the safety net.

For performance, the OS image build Cython-compiles the vendored modules with the image's own interpreter: `setup.py` builds them as extensions when `CYTHONIZE_DELTA_CALCULATOR=1` is set (the `python-instrument-cluster` Buildroot package sets it and provides `host-python-cython`). Python prefers the built `.so` over the `.py` next to it, so dev machines and tests (flag unset) transparently run the source.

### Shift points & the car databases

The LED ladder's shift point is computed, not tabulated: `ShiftPointCalculator`
(`core/vehicle/ecu.py`) finds where the next gear puts more torque at the
wheels than the current one. Gear ratios cancel out of that comparison, so
only the *shape* of the power curve matters — which is what makes the curve
measurable from telemetry alone, with no mass or drag constant.

Two db files feed it, both keyed by GT7 car id: `db/cars.json` (peak power and
torque with their rpm — no recorded provenance, and its `redline_rpm` column
is fabricated on 539 of 540 rows, so no curve is ever anchored on it) and
`db/car_classes.json` (aspiration, car type, category, engine layout, from
zetetos/gt-telemetry, MIT; regenerate with `tools/fetch_car_classes.py`). The
class picks how fast power falls away past the peak — the one thing the four
peak numbers on the wire cannot say, and the thing the shift point mostly
depends on. A sender's `power_to_limiter` still overrides it.

Every falloff value is measured off a real car rather than assumed: the app
captures full-throttle pulls behind the `accel_logging` config flag
(`core/vehicle/accel_recorder.py`) and the runs are fitted offline. The
constants in `ecu.py` carry the car, the pull count and the strength of the
evidence in their comments.
The measurements overturned the intuition the first priors encoded: in GT7
both turbos measured hold power nearly flat and the naturally aspirated V8 is
the one that droops.

Whether the computed shift point is actually *faster* than shifting on the
game's own light is a driving question, so the answer is measured on the
device: Setup → **Testing & Validation** opens a 0-to-100/200/300/400 m
timer (`core/vehicle/accel_timer.py`, driven by `states/accel_test_state.py`).
It arms itself when the car stops, starts on the launch and stops on the
target distance, so a run needs no screen taps — and it voids a run rather
than publishing a number that was measured differently from the one it is
being compared against (a pause, a car change, a gap in the stream, a car
that rolls to a stop). Distance is integrated from `car_speed`, the one
channel every feed carries; timing is on the *receiving* clock, because
`received_time`'s unit varies by reader. The clock is a
`DigitReadout` (`ui/widgets/base/digit_readout.py`) on the gauges' own
fixed digit grid (`ui/widgets/base/digits.py`, shared with `Widget`), so
hundredths ticking 100 times a second don't make the number squirm.
`tools/preview_accel_test.py` plays a synthetic pull through the real state
on any panel.

`rpm_alert.max` is a tachometer number, not the fuel cut — car 1461 declares
8000 and cuts at 7215 — so the controller learns the real limiter from the
engine (rpm falling while the pedal is down in an unchanged gear) and anchors
the target on that, never above the declared value. Without it the shift
target can sit above any rpm the engine can reach, which leaves the ladder
permanently unfinished.

### Desktop (PC) builds

`packaging/pyinstaller/Revokyte.spec` (+ `entry.py` beside it) builds the single-file desktop app; `.github/workflows/pc-release.yml` builds it for Windows and macOS on every `v*` tag and attaches the unsigned binaries to the tag's GitHub release. The spec must keep two things intact: package data (`assets/`, `db/`) collected into `instrument_cluster/` inside the bundle (fonts resolve via `importlib.resources`, `tracks.json` via `Path(__file__)`), and the packaged plugin `.py` files shipped **as data files** — `PluginManager` discovers them with `os.listdir`, then imports them as modules (covered by `collect_submodules` hidden imports). Local build: `pip install ".[pc]" pyinstaller && pyinstaller --noconfirm packaging/pyinstaller/Revokyte.spec`.

On desktop the dev display profile opens a resizable window (`pygame.SCALED | RESIZABLE`, vsync requested): pygame scales the fixed 1280×720 logical surface to the window and maps input back, so no app code sees the window size.

### Track Database

`db/tracks.json` is bundled and read-only — GT7 exposes no track/course ID in its telemetry, so it's sourced from the GT7 telemetry community instead of recorded on-device: each entry's start/finish `gate` (two points + crossing direction) and `bounds` (bounding box) come from [Bornhall/gt7telemetry](https://github.com/Bornhall/gt7telemetry)'s `gt7trackdetect.csv`, joined against [ddm999/gt7info](https://github.com/ddm999/gt7info)'s `course.csv` for names. A track not in the database shows the `---` placeholder.
