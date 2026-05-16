from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any

try:
    from openrgb import OpenRGBClient
    from openrgb.utils import RGBColor

    HAS_OPENRGB = True
except ImportError:
    HAS_OPENRGB = False


RGBTuple = tuple[int, int, int]

TICK_PHASES = [
    "idle",          # 0
    "llm_request",   # 1
    "reasoning",     # 2
    "proposal",      # 3
    "governance",    # 4
    "execution",     # 5
    "commit",        # 6
    "post",          # 7
]

HEALTH_COLORS: dict[str, RGBTuple] = {
    "idle": (0, 200, 255),
    "healthy": (0, 255, 80),
    "degraded": (255, 180, 0),
    "error": (255, 40, 40),
    "blocked": (255, 0, 120),
}

PHASE_DELAYS = [
    0.03,  # idle
    0.05,  # llm_request
    0.06,  # reasoning
    0.05,  # proposal
    0.09,  # governance
    0.06,  # execution
    0.11,  # commit
    0.04,  # post
]

_STAGE_TO_PHASE = {
    "runtime_initialized": 0,
    "before_pulse": 1,
    "before_model_reasoning": 2,
    "after_model_proposal": 3,
    "after_governance": 4,
    "before_execution": 5,
    "before_commit": 6,
    "after_pulse": 7,
    "runtime_closed": 0,
}


def pulse(color: RGBTuple, speed: float = 4.0, strength: float = 0.25) -> RGBTuple:
    t = time.time()
    wave = (math.sin(t * speed) + 1) / 2
    scale = 1.0 - strength + (wave * strength)
    return tuple(int(c * scale) for c in color)


def render_tick_frame(phase_index: int, health: str) -> list[RGBTuple]:
    frame: list[RGBTuple] = [(0, 0, 0)] * 8

    base = HEALTH_COLORS.get(health, (255, 255, 255))
    if health in {"degraded", "error", "blocked"}:
        color = pulse(base)
    else:
        color = base

    idx = max(0, min(7, int(phase_index)))
    frame[idx] = color
    return frame


def render_killswitch_frame() -> list[RGBTuple]:
    t = time.time()
    wave = (math.sin(t * 6) + 1) / 2
    brightness = int(255 * wave)
    return [(brightness, 0, 0)] * 8


def run_tick(runtime: Any, ledbus: Any) -> None:
    """Drop-in integration hook.

    runtime must expose:
      - get_phase_index() -> int (0-7)
      - get_health() -> str
      - is_killswitched() -> bool
    """
    if bool(runtime.is_killswitched()):
        ledbus.apply(render_killswitch_frame())
        return

    phase = int(runtime.get_phase_index())
    health = str(runtime.get_health())
    ledbus.apply(render_tick_frame(phase, health))


def phase_index_for_stage(stage: str) -> int:
    return _STAGE_TO_PHASE.get(str(stage), 0)


def derive_led_health(runtime: Any, stage: str) -> str:
    if bool(getattr(runtime, "is_killswitched", lambda: False)()):
        return "blocked"

    state = getattr(runtime, "state", None)
    if state is not None:
        if str(getattr(state, "execution_state", "")).lower() == "blocked":
            return "error"
        if bool(getattr(state, "tick_blocked_reason", None)):
            return "error"

    health = {}
    if hasattr(runtime, "get_health_signal"):
        try:
            health = dict(runtime.get_health_signal())
        except Exception:
            health = {}

    status = str(health.get("status", "healthy")).lower()
    if status == "blocked":
        return "blocked"
    if status == "degraded":
        return "degraded"

    if stage in {"runtime_initialized", "runtime_closed"}:
        return "idle"
    return "healthy"


@dataclass
class TickProjection:
    phase_index: int = 0
    health: str = "idle"
    killswitched: bool = False


class LEDBus:
    """Thin OpenRGB output adapter.

    Input contract: list of 8 RGB tuples where index is the logical LED slot.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 6742) -> None:
        self.client: Any | None = None
        self.device: Any | None = None
        self._enabled = False
        self.reason_unavailable: str | None = None

        if not HAS_OPENRGB:
            self.reason_unavailable = "python_openrgb_package_missing"
            return

        try:
            try:
                self.client = OpenRGBClient(ip=host, port=port)
            except TypeError:
                # Compatibility with older openrgb-python signatures.
                self.client = OpenRGBClient(host, port)
            self.device = self._find_device()
            self._enabled = self.device is not None
            if not self._enabled:
                self.reason_unavailable = "no_openrgb_keyboard_detected"
        except Exception:
            self._enabled = False
            self.reason_unavailable = "openrgb_connection_failed"

    @property
    def enabled(self) -> bool:
        return bool(self._enabled)

    def _find_device(self) -> Any | None:
        if self.client is None:
            return None
        try:
            for dev in self.client.devices:
                name = str(getattr(dev, "name", ""))
                if "keyboard" in name.lower() or "apex" in name.lower():
                    return dev
            return self.client.devices[0] if self.client.devices else None
        except Exception:
            return None

    def _emit_to_targets(self, frame: list[RGBTuple]) -> None:
        assert self.device is not None

        zones = list(getattr(self.device, "zones", []) or [])
        if zones:
            for zone, rgb in zip(zones[:8], frame):
                zone.set_color(RGBColor(*rgb))
            return

        leds = list(getattr(self.device, "leds", []) or [])
        for led, rgb in zip(leds[:8], frame):
            led.set_color(RGBColor(*rgb))

    def apply(self, frame: list[RGBTuple]) -> None:
        if not self.enabled or self.device is None:
            return
        try:
            self._emit_to_targets(frame)
        except Exception:
            # LED path must never block runtime execution.
            return

    def off(self) -> None:
        self.apply([(0, 0, 0)] * 8)


class TickLEDDriver:
    """Background sampler that projects runtime phase/health to a physical keyboard."""

    def __init__(self, ledbus: LEDBus, *, fps: float = 30.0) -> None:
        self.ledbus = ledbus
        self.fps = max(1.0, float(fps))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._target = TickProjection()
        self._display_phase = 0
        self._phase_started_at = time.monotonic()

    @property
    def enabled(self) -> bool:
        return self.ledbus.enabled

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        self.ledbus.off()

    def update_from_runtime(self, runtime: Any, stage: str) -> None:
        projection = TickProjection(
            phase_index=phase_index_for_stage(stage),
            health=derive_led_health(runtime, stage),
            killswitched=bool(getattr(runtime, "is_killswitched", lambda: False)()),
        )
        with self._lock:
            self._target = projection

    def _loop(self) -> None:
        frame_delay = 1.0 / self.fps
        while not self._stop.is_set():
            with self._lock:
                target = TickProjection(
                    phase_index=self._target.phase_index,
                    health=self._target.health,
                    killswitched=self._target.killswitched,
                )

            if target.killswitched:
                self.ledbus.apply(render_killswitch_frame())
                time.sleep(frame_delay)
                continue

            now = time.monotonic()
            if target.phase_index != self._display_phase:
                min_hold = PHASE_DELAYS[self._display_phase]
                if now - self._phase_started_at >= min_hold:
                    self._display_phase = max(0, min(7, int(target.phase_index)))
                    self._phase_started_at = now

            self.ledbus.apply(render_tick_frame(self._display_phase, target.health))
            time.sleep(frame_delay)
