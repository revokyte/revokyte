import os
import signal
import sys
import time

import pygame

from .addons.feeds import feed_needs_reinstall
from .config import Config, ConfigManager
from .core.plugin_system.plugin_manager import PluginManager
from .core.system import unhealthy
from .core.system.wifi_manager import WifiManager
from .core.vehicle.accel_recorder import (
    AccelRunRecorder,
    AccelRunStore,
    default_runs_dir,
)
from .core.vehicle.vehicle_bus import VehicleBus
from .extensions import runtime as extensions
from .logger import Logger
from .peripherals.display import Display
from .signals.signal_pipeline import SignalPipeline
from .states.gate import entry_state
from .states.state_manager import StateManager
from .telemetry.mode import TelemetryMode
from .ui.feed_update_window import FeedUpdateWindow
from .ui.no_signal_window import NoSignalWindow
from .ui.views.registry import core_views, views
from .ui.wifi_status_window import WifiStatusWindow
from .ui.window_layering import WindowManager

logger = Logger("InstrumentClusterOS").get()

# How long main() will wait for the volume holding the config to appear
# before giving up and reading defaults. Sized to the worst /data ext4
# journal replay observed after a power cut (~3.5 s) with slack on top.
_CONFIG_VOLUME_TIMEOUT = 10.0


def _wait_for_config_volume(timeout: float = _CONFIG_VOLUME_TIMEOUT) -> None:
    """Block until the volume holding the config file is mounted.

    On the appliance the config lives on /data, and the service starts
    before local-fs.target on purpose: the seconds of Python/pygame imports
    above overlap the ext4 journal replay /data needs after a power cut.
    By the time the imports finish the mount is usually there; when it
    isn't, wait here — at the last moment — instead of silently reading
    defaults off the bare mountpoint directory. Dev machines (config under
    $HOME) skip this entirely.
    """
    if not str(ConfigManager.path).startswith("/data/"):
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.ismount("/data"):
            return
        time.sleep(0.05)
    logger.warning(
        "/data not mounted after %.0f s; config may fall back to defaults", timeout
    )


def run(conf: Config) -> None:
    # no real audio device on the Pi
    os.environ["SDL_AUDIODRIVER"] = "dummy"

    def handle_exit(sig, frame):
        logger.info("Exit signal received. Closing dashboard ...")
        state_manager.is_running = False

    # register OS signals for systemd compatibility
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    pygame.init()
    pygame.mouse.set_visible(False)

    display = None
    crashed = False
    try:
        # Initialize the rendering pipeline. Display resolves which physical
        # panel we're driving and registers it process-wide so input mapping
        # (button.py) can translate physical input into logical coordinates
        # and the per-resolution skin (ui/skins) resolves. The whole UI is
        # drawn into a native-resolution logical surface; Display presents
        # it to the panel (rotation only, no resampling).
        display = Display(getattr(conf, "display", "auto"))
        main_surface = display.surface

        # A feed installed by an earlier image survives on /data, so it can
        # be a build this image was never tested against. Say so in the log —
        # on a release image with no SSH the Setup row is the other half of
        # this warning.
        stale_feed = feed_needs_reinstall(
            conf.telemetry_feed, conf.telemetry_feed_version
        )
        if stale_feed is not None and conf.telemetry_mode != TelemetryMode.DEMO.value:
            logger.warning(
                "Installed %s feed is %r but this image pins %s — "
                "re-run Setup to update it.",
                stale_feed.label,
                conf.telemetry_feed_version or "unknown",
                stale_feed.version,
            )

        vehicle_bus = VehicleBus()

        plugin_dir = os.path.join(os.path.dirname(__file__), "plugins")
        plugin_manager = PluginManager(plugin_dir, vehicle_bus)

        signal_pipeline = SignalPipeline()

        state_manager = StateManager(main_surface, vehicle_bus)
        # States reach the plugin manager through the state manager (e.g.
        # a settings change that rebuilds the plugin layout).
        state_manager.plugin_manager = plugin_manager

        # Automotive-style window layering: the state manager is
        # the BASE layer. Overlay windows are composited above
        # it every frame (refer to ui/window_layering.py).
        window_manager = WindowManager(state_manager)
        # Telemetry link loss. The one overlay the base build registers
        # itself: it must never be drawn over, which is exactly what the
        # SYSTEM_ALERT layer guarantees.
        window_manager.add_window(NoSignalWindow(vehicle_bus, state_manager))
        # Stale-feed notice, once per boot. Constructed unconditionally; it
        # decides at build time whether it has anything to say.
        window_manager.add_window(
            FeedUpdateWindow(conf, state_manager, main_surface.get_size())
        )

        # Installed extension distributions wire themselves in here; with
        # none installed this is a silent no-op. Wired before load_plugins
        # so an extension can install its feature provider on the plugin
        # manager first (see extensions.py).
        extensions.load(
            vehicle_bus=vehicle_bus,
            state_manager=state_manager,
            window_manager=window_manager,
            plugin_manager=plugin_manager,
        )
        plugin_manager.load_plugins()

        # Build every view before the first frame, so no screen transition
        # ever allocates one. This is the whole point of the ViewRegistry:
        # view surfaces are allocated in C by SDL, invisible to a collector
        # driven by Python object counts, so per-transition churn used to
        # accumulate unseen and then cost a ~1 s gen-2 collection at an
        # arbitrary moment — potentially mid-corner. After extensions.load()
        # so extension-declared views (when that lands) join the same pass.
        #
        # Measured at ~115 ms added on a Pi 4, under 1% of boot. If a future
        # view is heavy enough to change that, delete this line: acquire()
        # falls back to building on first use.
        # Extension-declared views join the same pass: discovery is
        # entry-point based, so this is the first moment the full view set is
        # known. A view that fails takes its Setup row with it — fail-open
        # must not leave a button that opens nothing.
        unhealthy.clear()
        views.preload(core_views() + tuple(extensions.view_classes))
        extensions.drop_rows_missing_views(views.failed)

        # Wi-Fi never gates the dashboard. With credentials provisioned the
        # boot goes straight to the gauges and association completes in the
        # background — WifiStatusWindow shows a small "connecting" pill until
        # it does. This is deliberate for the trackside power-cut case: the
        # router may come up minutes after the dash, and the old blocking
        # connecting screen (15 s poll + 5 s minimum display) held the
        # dashboard hostage to it. Only a true first boot — nothing
        # provisioned on /data (no network block, or only the flash
        # template's unedited placeholder), so association could never
        # succeed — goes to Wi-Fi setup. On dev machines (no wlan0) neither
        # branch triggers.
        wifi = WifiManager()
        if wifi.available and not wifi.is_associated() and not wifi.has_credentials():
            logger.info("No Wi-Fi credentials provisioned; showing setup.")
            from .states.wifi_setup_state import ENTRY_BOOT, WifiSetupState

            state_manager.push_state(
                WifiSetupState(
                    state_manager,
                    manager=wifi,
                    entry=ENTRY_BOOT,
                    plugin_manager=plugin_manager,
                    pipeline=signal_pipeline,
                )
            )
        else:
            window_manager.add_window(WifiStatusWindow(wifi, state_manager))
            state_manager.push_state(
                entry_state(state_manager, signal_pipeline, plugin_manager)
            )

        # notify systemd that initialization is complete
        vehicle_bus.health.notify_ready()

        clock = pygame.time.Clock()

        # Field diagnostics: a stalled main loop dumps its own stack to the
        # log, and touch arrivals leave a (rate-limited) trace — together
        # they separate "input not delivered" from "input swallowed" from
        # "loop blocked" on a device with no debugger.
        import threading as _threading

        from .debug.stall_detector import StallDetector

        stall_detector = StallDetector(_threading.get_ident())
        stall_detector.start()
        last_touch_log = 0.0

        # Opt-in (config: accel_logging), off on every shipped device. The
        # dyno needs full-throttle pulls, and on the appliance this is the
        # only place they can be captured: the feed emits to loopback and
        # the cluster itself owns that port, so no second listener can bind.
        accel_recorder = (
            AccelRunRecorder(AccelRunStore(default_runs_dir()))
            if ConfigManager.get_config().accel_logging
            else None
        )
        if accel_recorder is not None:
            logger.info("accel logging on — runs go to %s", default_runs_dir())

        # Smoke test of the frozen desktop builds (pc-release.yml): run this
        # many frames, then shut down as if asked to. Unset on every real
        # launch.
        exit_after_frames = int(os.environ.get("IC_EXIT_AFTER_FRAMES", "0"))
        frames = 0

        while state_manager.is_running:
            dt = clock.tick(60) / 1000
            stall_detector.beat()

            vehicle_bus.tick(dt)
            vehicle_bus.merge_signals(extensions.update_signals())
            signal_pipeline.update(vehicle_bus, dt)
            if accel_recorder is not None:
                # A dead link ends the pull. Readers hold their last frame
                # forever, so a stream that just stops — session over, console
                # asleep — otherwise leaves the run open and unwritten: the
                # recorder only ever closes one on a lift, an upshift or the
                # limiter, and none of those arrive after the frames stop.
                accel_recorder.feed(
                    None
                    if vehicle_bus.signals.get("telemetry_stale")
                    else vehicle_bus.frame
                )
                run = accel_recorder.take_result()
                if run is not None:
                    # There is no screen for this; the journal is the report.
                    logger.info(
                        "accel run %s: gear %d  %d-%d rpm  %d samples  (%s)",
                        "kept" if run.accepted else "dropped",
                        run.gear,
                        run.rpm_lo,
                        run.rpm_hi,
                        run.sample_count,
                        run.reason,
                    )
            # Plugin reloads may be requested from background threads
            # (extensions do) but execute here — pygame surfaces must be
            # created on the main thread.
            if plugin_manager.consume_reload_request():
                plugin_manager.reload_plugins()
            plugin_manager.update(dt)

            for event in pygame.event.get():
                if event.type in (pygame.FINGERDOWN, pygame.MOUSEBUTTONDOWN):
                    now = time.monotonic()
                    if now - last_touch_log >= 1.0:
                        last_touch_log = now
                        # logger.debug("touch down received")
                if event.type == pygame.QUIT:
                    state_manager.is_running = False
                elif event.type in (
                    pygame.WINDOWEXPOSED,
                    pygame.WINDOWRESTORED,
                    pygame.WINDOWSIZECHANGED,
                ):
                    # The OS invalidated our pixels; one-shot dirty sprites
                    # won't repaint on their own.
                    state_manager.request_full_paint()
                window_manager.handle_event(event)

            window_manager.update(dt)
            display.present(window_manager.draw(main_surface))

            frames += 1
            if frames == exit_after_frames:
                state_manager.is_running = False

    except Exception:
        logger.exception("Critical system error")
        # don't sys.exit here, let finally clean up first
        crashed = True

    finally:
        logger.info("Cleaning up resources...")
        extensions.stop()
        # Capture any in-memory config changes that were never queued (e.g.
        # quitting while a settings view is still open — shutdown doesn't
        # unwind the state stack, so no exit() runs) and wait for the
        # background writer to drain. Runs after extensions.stop() so
        # extension stop hooks can still mutate config. A no-change persist
        # is skipped by the writer, so this is free in the common case.
        ConfigManager.persist()
        if not ConfigManager.flush(timeout=2.0):
            logger.warning("Config flush timed out; latest changes may not be on disk")
        if display is not None:
            display.close()
        pygame.quit()

    if crashed:
        # Cleaned up; now make the failure visible to whoever launched us. A
        # zero exit here let desktop builds that died at startup ship for
        # releases on end: no crash report, no failed CI step, nothing.
        sys.exit(1)


def main() -> None:
    try:
        _wait_for_config_volume()
        run(ConfigManager.get_config())
    except Exception:
        logger.critical("Application failed to start", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
