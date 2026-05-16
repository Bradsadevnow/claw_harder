from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .expression import MorphChannel


@dataclass
class ShellNote:
    role: str
    content: str


def rich_shell_is_available() -> bool:
    try:
        import rich  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


class RichShellSession:
    def __init__(
        self,
        runtime: Any,
        *,
        agent_name: str,
        morph_channel: MorphChannel,
        console_url: str | None = None,
        dashboard_url: str | None = None,
        morph_width: int = 28,
        morph_height: int = 12,
    ) -> None:
        self.runtime = runtime
        self.agent_name = agent_name
        self.morph_channel = morph_channel
        self.console_url = console_url
        self.dashboard_url = dashboard_url
        self.morph_width = max(20, morph_width)
        self.morph_height = max(8, morph_height)
        self.notes: list[ShellNote] = []

    def add_note(self, role: str, content: str) -> None:
        text = content.strip()
        if not text:
            return
        self.notes.append(ShellNote(role=role, content=text))
        self.notes = self.notes[-8:]

    def render(self) -> Any:
        from rich.align import Align
        from rich.console import Group
        from rich.layout import Layout
        from rich.panel import Panel

        layout = Layout(name="root")
        layout.split_column(
            Layout(name="top", size=max(self.morph_height + 4, 14)),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=4),
        )
        layout["top"].split_row(
            Layout(name="avatar", size=24),
            Layout(name="morph", size=self.morph_width + 6),
            Layout(name="status"),
        )

        layout["top"]["avatar"].update(
            Panel(
                Align.center(self._render_avatar(), vertical="middle"),
                title="AVATAR",
                border_style="blue",
            )
        )
        layout["top"]["morph"].update(
            Panel(
                Align.center(self._render_morph(), vertical="middle"),
                title="MORPH",
                border_style="magenta",
            )
        )
        layout["top"]["status"].update(
            Panel(
                self._render_status(),
                title="RUNTIME",
                border_style="cyan",
            )
        )
        layout["body"].update(
            Panel(
                self._render_chat(),
                title="Interaction",
                border_style="green",
            )
        )
        layout["footer"].update(
            Panel(
                Group(*self._render_footer()),
                title="Input",
                border_style="yellow",
            )
        )
        return layout

    def _render_avatar(self) -> str:
        status = self.runtime.status_snapshot()
        frame = self.morph_channel.current()
        mood = frame.mood if frame is not None else "steady"
        posture = str(status.get("posture", "RESPONSIVE")).strip().lower()
        killswitch = bool(status.get("killswitch_engaged", False))
        cycle = int(status.get("cycle", 0))
        signal = _snapshot_signal_vector(self.runtime)
        art = _avatar_art(
            posture=posture,
            mood=mood,
            killswitch=killswitch,
            phase=cycle % 2,
            signal=signal,
        )
        return _center_block(art, width=18, height=self.morph_height)

    def _render_morph(self) -> str:
        frame = self.morph_channel.current()
        mood = frame.mood if frame is not None else "steady"
        glyph = frame.glyph if frame is not None else ". ."
        hint = frame.hint if frame is not None else "transient expression only"
        art = _morph_art(mood, glyph)
        block = list(art)
        if hint:
            block.extend(["", f"[ {hint} ]"])
        return _center_block(block, width=self.morph_width, height=self.morph_height)

    def _render_status(self) -> Any:
        from rich.table import Table
        from rich.text import Text

        status = self.runtime.status_snapshot()
        health = self.runtime.get_health_signal()
        mode = str(status.get("execution_mode", "plan")).upper()
        authority = str(status.get("operator_authority", "user")).upper()
        governance = str(status.get("governance_mode", "standard")).upper()
        drift_ratio = float(health.get("drift_ratio", 0.0))
        health_status = str(status.get("health_status", "healthy")).upper()
        killswitch = bool(status.get("killswitch_engaged", False))

        table = Table.grid(padding=(0, 2))
        table.add_column(justify="left", ratio=1)
        table.add_column(justify="right", ratio=1)
        table.add_row(Text(self.agent_name, style="bold white"), Text(mode, style="bold green" if mode == "EXECUTE" else "bold yellow"))
        table.add_row(Text(governance, style="cyan"), Text(authority, style="bold magenta" if authority == "BUILDER" else "bold blue"))
        table.add_row(Text(f"drift: {drift_ratio:.2f}", style="white"), Text(f"tick: {int(status.get('tick_count', 0))}", style="white"))
        table.add_row(Text(f"health: {health_status}", style="red" if health_status == "BLOCKED" else "green"), Text("KILLSWITCH" if killswitch else "ONLINE", style="bold red" if killswitch else "bold green"))
        if self.console_url:
            table.add_row(Text("api", style="dim"), Text(self.console_url, style="dim"))
        if self.dashboard_url:
            table.add_row(Text("hud", style="dim"), Text(self.dashboard_url, style="dim"))
        return table

    def _render_chat(self) -> Any:
        from rich.console import Group
        from rich.text import Text

        entries: list[Any] = []
        for frame in self.runtime.state.memory.recent(8):
            entries.append(_render_chat_line(frame.role, frame.content, self.agent_name))
        for note in self.notes[-4:]:
            entries.append(_render_chat_line(note.role, note.content, self.agent_name))
        if not entries:
            entries.append(Text("No conversation yet.", style="dim"))
        return Group(*entries)

    def _render_footer(self) -> list[Any]:
        from rich.text import Text

        tick_mode = self.runtime.status_snapshot().get("tick_mode", "off")
        lines = [
            Text("> message her directly, or use /plan /execute /mode /authority /kill /unkill", style="bold yellow"),
        ]
        if tick_mode != "off":
            lines.append(Text(f"Tick controller active: {tick_mode}. /tick off to pause.", style="yellow"))
        else:
            lines.append(Text("Avatar is expressive only. Status is truth.", style="dim"))
        return lines


def run_rich_shell(
    runtime: Any,
    *,
    agent_name: str,
    morph_channel: MorphChannel,
    command_handler: Any,
    console_url: str | None = None,
    dashboard_url: str | None = None,
    morph_width: int = 28,
    morph_height: int = 12,
) -> None:
    from rich.console import Console
    from rich.live import Live

    console = Console()
    session = RichShellSession(
        runtime,
        agent_name=agent_name,
        morph_channel=morph_channel,
        console_url=console_url,
        dashboard_url=dashboard_url,
        morph_width=morph_width,
        morph_height=morph_height,
    )
    session.add_note("runtime", "The CLI was a pipe. This is a system.")

    if runtime.should_resume():
        for line in runtime.emit_resume():
            session.add_note("assistant", line)

    with Live(session.render(), console=console, screen=True, auto_refresh=False) as live:
        while True:
            live.update(session.render(), refresh=True)
            live.stop()
            try:
                user_input = console.input("\n> ").strip()
            except EOFError:
                console.print()
                break
            finally:
                live.start()

            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit"}:
                break
            if user_input.startswith("/"):
                should_continue, output = command_handler(user_input)
                if output:
                    session.add_note("runtime", output)
                if not should_continue:
                    break
                continue

            reply = runtime.run_single_cycle(user_input)
            session.add_note("user", user_input)
            session.add_note("assistant", reply)


def _render_chat_line(role: str, content: str, agent_name: str) -> Any:
    from rich.text import Text

    label_map = {
        "assistant": agent_name,
        "runtime": "Runtime",
        "system": "System",
        "user": "You",
    }
    style_map = {
        "assistant": "bold green",
        "runtime": "bold yellow",
        "system": "bold yellow",
        "user": "bold cyan",
    }
    text = Text()
    text.append(f"{label_map.get(role, role.title())}: ", style=style_map.get(role, "bold white"))
    text.append(content, style="white")
    return text


def _morph_art(mood: str, glyph: str) -> list[str]:
    mood_map = {
        "bright": [
            "   .-.   ",
            "  (^ ^)  ",
            "   '-'   ",
        ],
        "curious": [
            "   .-.   ",
            "  (o o)  ",
            "   '?'   ",
        ],
        "guarded": [
            "   .-.   ",
            "  (- -)  ",
            "   /_\\   ",
        ],
        "intent": [
            "   .-.   ",
            "  (> <)  ",
            "   /_\\   ",
        ],
        "thinking": [
            "   .-.   ",
            "  (- -)  ",
            "   ...   ",
        ],
        "steady": [
            "   .-.   ",
            "  (o -)  ",
            "   '-'   ",
        ],
    }
    art = list(mood_map.get(mood, mood_map["steady"]))
    art.append("")
    art.extend(glyph.splitlines())
    return art


def _avatar_art(*, posture: str, mood: str, killswitch: bool, phase: int, signal: dict[str, float]) -> list[str]:
    if killswitch:
        return [
            "  .-X-.  ",
            " ( x x ) ",
            "  /|_|\\  ",
            "   / \\   ",
            "",
            " KILLSWITCH ",
        ]

    valence = signal.get("valence", 0.0)         # -1..1
    arousal = signal.get("arousal", 0.0)         # 0..1
    instability = signal.get("instability", 0.0) # 0..1
    threat = signal.get("threat", 0.0)
    calm = signal.get("calm", 0.0)
    curiosity = signal.get("curiosity", 0.0)
    joy = signal.get("joy", 0.0)
    frustration = signal.get("frustration", 0.0)

    eye_map = {
        "bright": "^ ^",
        "curious": "o ?",
        "guarded": "- -",
        "intent": "> <",
        "thinking": "- .",
        "steady": "o -",
    }
    if threat > 0.55 or frustration > 0.45:
        eyes = "x x" if instability > 0.7 else "- !"
    elif curiosity > 0.7:
        eyes = "o ?"
    elif calm > 0.75:
        eyes = "- -"
    else:
        eyes = eye_map.get(mood, "o -")

    if valence >= 0.3 or joy >= 0.6:
        mouth = "u"
    elif valence <= -0.3 or frustration >= 0.5:
        mouth = "_"
    else:
        mouth = "~" if posture == "focused" else "-"

    if posture == "latent":
        arms = "  | |   " if arousal < 0.45 else " /| |\\  "
    elif posture == "responsive":
        arms = " /| |\\  " if (phase == 0 or arousal > 0.6) else "  | |   "
    elif posture == "focused":
        arms = " /|_|\\  "
    else:
        arms = " \\|_|/  " if (phase == 0 or arousal > 0.65) else " /|_|\\  "

    stance = " / | \\  " if arousal > 0.55 else " /   \\  "
    badge = {
        "latent": "latent",
        "responsive": "awake",
        "focused": "locked",
        "deliberative": "deep",
    }.get(posture, posture)
    dominant = _dominant_signal(signal)
    valence_tag = "+" if valence >= 0 else "-"
    vector_tag = f"v{valence_tag}{abs(valence):.1f} a{arousal:.1f}"

    return [
        "  .---.  ",
        f" ({eyes}) ",
        f"   {mouth}    ",
        arms,
        stance,
        f" [{badge}] ",
        f" {dominant[:8]:<8}",
        f" {vector_tag:<8}",
    ]


def _snapshot_signal_vector(runtime: Any) -> dict[str, float]:
    signal = getattr(getattr(runtime, "state", None), "signal", None)
    core = getattr(signal, "core", {}) if signal is not None else {}

    def _u(value: Any, default: float = 0.0) -> float:
        if isinstance(value, bool):
            return default
        if isinstance(value, (int, float)):
            v = float(value)
        else:
            return default
        if v != v:  # NaN guard
            return default
        return max(0.0, min(1.0, v))

    def _s(value: Any, default: float = 0.0) -> float:
        if isinstance(value, bool):
            return default
        if isinstance(value, (int, float)):
            v = float(value)
        else:
            return default
        if v != v:  # NaN guard
            return default
        return max(-1.0, min(1.0, v))

    return {
        "valence": _s(getattr(signal, "valence", 0.0), 0.0),
        "arousal": _u(getattr(signal, "arousal", 0.0), 0.0),
        "instability": _u(getattr(signal, "instability", 0.0), 0.0),
        "threat": _u(core.get("threat", 0.0), 0.0),
        "calm": _u(core.get("calm", 0.0), 0.0),
        "curiosity": _u(core.get("curiosity", 0.0), 0.0),
        "joy": _u(core.get("joy", 0.0), 0.0),
        "frustration": _u(core.get("frustration", 0.0), 0.0),
        "trust": _u(core.get("trust", 0.0), 0.0),
    }


def _dominant_signal(signal: dict[str, float]) -> str:
    candidates = (
        ("threat", signal.get("threat", 0.0)),
        ("frustration", signal.get("frustration", 0.0)),
        ("curiosity", signal.get("curiosity", 0.0)),
        ("calm", signal.get("calm", 0.0)),
        ("joy", signal.get("joy", 0.0)),
        ("trust", signal.get("trust", 0.0)),
    )
    top_name, top_score = max(candidates, key=lambda item: item[1])
    if top_score < 0.2:
        return "neutral"
    return top_name


def _center_block(lines: list[str], *, width: int, height: int) -> str:
    safe_lines = [line[: max(1, width - 2)] for line in lines]
    total = len(safe_lines)
    if total < height:
        top_pad = max(0, (height - total) // 2)
        bottom_pad = max(0, height - total - top_pad)
        safe_lines = ([""] * top_pad) + safe_lines + ([""] * bottom_pad)
    return "\n".join(_center_line(line, width) for line in safe_lines[:height])


def _center_line(line: str, width: int) -> str:
    if len(line) >= width:
        return line[:width]
    left = (width - len(line)) // 2
    right = width - len(line) - left
    return (" " * left) + line + (" " * right)
