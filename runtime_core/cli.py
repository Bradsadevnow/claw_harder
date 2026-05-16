from __future__ import annotations

import argparse
import errno
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import sleep, time
from typing import Any

from .agent_profile import load_agent_profile
from .comms_server import CommsServer
from .expression import ExpressionTapModel, MorphChannel
from .model import ModelManager, load_manager_from_config, load_model
from .panel import panel_is_available
from .runtime import RuntimeRuntime, RED_TEAM_BASELINE
from .tools import example_tool_snippet
from .ui_bridge import launch_panel_process

try:
    from .roadmap_server import RoadmapServer
except ImportError:
    RoadmapServer = CommsServer


LOCKED_MODEL_NAME = "openai/gpt-oss-20b"
LOCKED_CONTEXT_WINDOW_CAP = 60000


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


@dataclass
class DashboardHandle:
    url: str
    _server: ThreadingHTTPServer | None = None
    _thread: Thread | None = None
    _process: subprocess.Popen[str] | None = None

    def stop(self) -> None:
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
            self._process = None
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Runtime runtime chat loop")
    parser.set_defaults(panel=False, console_server=True, dashboard=False, openrgb=True)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--state-path", default=".runtime/state.json")
    parser.add_argument("--log-path", default=".runtime/events.jsonl")
    parser.add_argument("--status-path", default=".runtime/status.json")
    parser.add_argument("--mcp-config", default="mcp_servers.json")
    parser.add_argument(
        "--panel",
        dest="panel",
        action="store_true",
        help="Launch the legacy Runtime observer panel as a separate process.",
    )
    parser.add_argument("--no-panel", dest="panel", action="store_false", help="Disable the legacy observer panel launch.")
    parser.add_argument(
        "--console-server",
        dest="console_server",
        action="store_true",
        help="Launch the roadmap console server alongside the CLI (default).",
    )
    parser.add_argument(
        "--no-console-server",
        dest="console_server",
        action="store_false",
        help="Disable the roadmap console server launch.",
    )
    parser.add_argument("--console-host", default="127.0.0.1")
    parser.add_argument("--console-port", type=int, default=8047)
    parser.add_argument(
        "--dashboard",
        dest="dashboard",
        action="store_true",
        help="Launch the web dashboard alongside the CLI.",
    )
    parser.add_argument(
        "--no-dashboard",
        dest="dashboard",
        action="store_false",
        help="Disable web dashboard launch.",
    )
    parser.set_defaults(dashboard=False)
    parser.add_argument("--dashboard-host", default="127.0.0.1")
    parser.add_argument("--dashboard-port", type=int, default=4173)
    parser.add_argument("--dashboard-dir", default="monitor_dashboard")
    parser.add_argument("--console-config-path", default="runtime_capabilities.md")
    parser.add_argument("--console-model", default=None)
    parser.add_argument("--console-base-url", default=None)
    parser.add_argument(
        "--openrgb",
        dest="openrgb",
        action="store_true",
        help="Enable OpenRGB keyboard projection for runtime phase and health (default).",
    )
    parser.add_argument(
        "--no-openrgb",
        dest="openrgb",
        action="store_false",
        help="Disable OpenRGB keyboard projection startup.",
    )
    parser.add_argument("--openrgb-host", default="127.0.0.1")
    parser.add_argument("--openrgb-port", type=int, default=6742)
    parser.add_argument(
        "--provider",
        default="lmstudio",
        choices=["auto", "demo", "xai", "openai", "openai-compatible", "lmstudio"],
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key-env")
    parser.add_argument("--profile", default=None, help="Path to agent profile JSON for continuity resume")
    parser.add_argument("--model-config", default=None, help="Path to models.json for named model registry")
    return parser


def load_dotenv(path: Path) -> None:
    if not path.exists() or not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        os.environ.setdefault(key, value)


def print_banner(
    runtime: RuntimeRuntime,
    agent_name: str,
    *,
    console_url: str | None = None,
    dashboard_url: str | None = None,
) -> None:
    print(f"{agent_name} runtime chat loop")
    print("Type `exit` to stop.")
    print("Type `/help-tools` to see how to add tools.")
    print("Type `/tools` to list loaded tools.")
    print("Type `/mode` to show governance mode or `/mode expert` to switch.")
    print("Type `/authority` to show operator authority or `/authority builder` to switch.")
    print("Type `/plan` or `/execute` to toggle tool execution mode.")
    print("Type `/kill` for an immediate runtime stop or `/unkill` to release it.")
    print("When a tool needs approval, reply with `yes` or `no` in the next turn.")
    print("Type `/model` to show active model or `/model list` to see all.")
    if runtime.profile is not None:
        print(f"Profile: {runtime.profile.name} ({runtime.profile.mode})")
    if runtime.mcp_tools:
        print(f"MCP tools loaded: {', '.join(runtime.mcp_tools)}")
    print(f"Governance mode: {runtime.state.governance.mode}")
    exec_label = runtime.state.execution_mode
    print(f"Execution mode: {exec_label}")
    descriptor = runtime.model.descriptor()
    if hasattr(runtime.model, "active_name") and hasattr(runtime.model, "names"):
        print(f"Model (active): {runtime.model.active_name} — {descriptor['model']} [{descriptor['provider']}]")
        print(f"Models available: {', '.join(runtime.model.names())}")
    else:
        print(f"Model provider: {descriptor['provider']}")
        print(f"Model: {descriptor['model']}")
        if descriptor["base_url"]:
            print(f"Base URL: {descriptor['base_url']}")
    print(f"Context cap: {LOCKED_CONTEXT_WINDOW_CAP}")
    if dashboard_url:
        print(f"Open dashboard: {dashboard_url}")
    if console_url:
        print(f"API backend (internal): {console_url}")


def validate_output_path(path: Path, label: str) -> None:
    if path.exists() and path.is_dir():
        raise ValueError(f"{label} must be a file path, got directory: {path}")


def resolve_model_name(cli_model: str | None) -> str:
    requested = (
        cli_model
        or os.environ.get("LMSTUDIO_MODEL")
        or os.environ.get("RUNTIME_MODEL")
        or os.environ.get("OPENAI_MODEL")
    )
    if requested and requested != LOCKED_MODEL_NAME:
        raise ValueError(
            f"Runtime is locked to {LOCKED_MODEL_NAME!r} during the current roadmap phase; got {requested!r}."
        )
    return LOCKED_MODEL_NAME


def maybe_start_console_server(args: argparse.Namespace, *, runtime: RuntimeRuntime) -> RoadmapServer | None:
    if not getattr(args, "console_server", False):
        return None

    def _start(port: int) -> RoadmapServer:
        server = RoadmapServer(runtime, host=args.console_host, port=port)
        server.start()
        return server

    try:
        return _start(args.console_port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE and int(args.console_port) != 0:
            print(f"Console port {args.console_port} is in use. Falling back to an available port.")
            try:
                return _start(0)
            except Exception as fallback_exc:
                print(f"Console server not started: {fallback_exc}")
                return None
        print(f"Console server not started: {exc}")
        return None
    except Exception as exc:
        print(f"Console server not started: {exc}")
        return None


def _npm_command() -> str | None:
    direct = shutil.which("npm")
    if direct:
        return direct
    fallback = Path("/home/brad/projects/.tooling/node/bin/npm")
    if fallback.exists():
        return str(fallback)
    return None


def _resolve_dashboard_dir(raw_dir: str) -> Path:
    requested = Path(raw_dir).expanduser()
    if requested.is_absolute():
        return requested.resolve()

    cwd_candidate = (Path.cwd() / requested).resolve()
    package_root_candidate = (Path(__file__).resolve().parent.parent / requested).resolve()
    for candidate in (cwd_candidate, package_root_candidate):
        if candidate.exists():
            return candidate
    return cwd_candidate


def _start_static_dashboard(*, host: str, port: int, dist_dir: Path) -> DashboardHandle:
    handler = partial(_QuietStaticHandler, directory=str(dist_dir))
    server = ThreadingHTTPServer((host, port), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    bound_host, bound_port = server.server_address[:2]
    return DashboardHandle(url=f"http://{bound_host}:{bound_port}", _server=server, _thread=thread)


def _start_vite_dashboard(
    *,
    host: str,
    port: int,
    dashboard_dir: Path,
    console_url: str,
) -> DashboardHandle:
    npm_cmd = _npm_command()
    if npm_cmd is None:
        raise RuntimeError("npm not found and dashboard dist/ is missing.")
    env = os.environ.copy()
    env["VITE_RUNTIME_API_BASE"] = console_url
    node_bin = Path("/home/brad/projects/.tooling/node/bin")
    if node_bin.exists():
        env["PATH"] = f"{node_bin}:{env.get('PATH', '')}"
    process = subprocess.Popen(
        [npm_cmd, "run", "dev", "--", "--host", host, "--port", str(port), "--strictPort"],
        cwd=str(dashboard_dir),
        env=env,
    )
    sleep(0.5)
    if process.poll() is not None:
        raise RuntimeError("Dashboard dev server exited immediately.")
    return DashboardHandle(url=f"http://{host}:{port}", _process=process)


def _pick_open_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def maybe_start_dashboard(
    args: argparse.Namespace,
    *,
    console_url: str | None,
) -> DashboardHandle | None:
    if not getattr(args, "dashboard", False):
        return None

    if console_url is None:
        print("Dashboard not started: console server is unavailable, so the dashboard has no API backend.")
        return None

    dashboard_dir = _resolve_dashboard_dir(str(args.dashboard_dir))
    if not dashboard_dir.exists():
        print(f"Dashboard not started: directory not found at {dashboard_dir}")
        return None

    preferred_port = int(args.dashboard_port)
    try:
        return _start_vite_dashboard(
            host=args.dashboard_host,
            port=preferred_port,
            dashboard_dir=dashboard_dir,
            console_url=console_url,
        )
    except Exception as exc:
        fallback_port = _pick_open_port(str(args.dashboard_host))
        print(
            f"Dashboard port {preferred_port} unavailable. "
            f"Retrying dashboard on {fallback_port}."
        )
        try:
            return _start_vite_dashboard(
                host=args.dashboard_host,
                port=fallback_port,
                dashboard_dir=dashboard_dir,
                console_url=console_url,
            )
        except Exception as retry_exc:
            print(f"Dashboard dev server not started: {retry_exc}")

    dist_dir = dashboard_dir / "dist"
    if dist_dir.exists() and (dist_dir / "index.html").exists():
        if not console_url.endswith(":8047"):
            print(
                "Dashboard static fallback skipped: static build defaults to API port 8047, "
                f"but console is running at {console_url}."
            )
            return None
        try:
            return _start_static_dashboard(host=args.dashboard_host, port=args.dashboard_port, dist_dir=dist_dir)
        except Exception as exc:
            print(f"Dashboard static server not started: {exc}")

    print("Dashboard not started: no usable dashboard launch path found.")
    return None


def get_avatar_glyph(health: dict[str, Any]) -> str:
    """Derive the ASCII avatar based on health and signal state."""
    issue = health.get("issue", False)
    drift = health.get("drift", False)
    signal = health.get("signal", {})

    if issue:
        return "( ! _ ! )"
    if drift:
        return "( •_•~ )"

    threat = signal.get("threat", 0.0)
    wonder = signal.get("wonder", 0.0)
    curiosity = signal.get("curiosity", 0.8)

    if threat > 0.7:
        return "( >_< )"
    if wonder > 0.7 or curiosity > 0.75:
        return "( •̀ ᴗ •́ )"

    return "( •_• )"


def render_status_line(health: dict[str, Any], mode_label: str) -> str:
    cycle = health.get("cycle", 0)
    active = health.get("active_nodes", 0)
    provisional = health.get("provisional_nodes", 0)
    drift = health.get("drift", False)
    issue = health.get("issue", False)
    killswitch = health.get("killswitch_engaged", False)

    avatar = get_avatar_glyph(health)

    if killswitch:
        symbol = "X"
    elif drift:
        symbol = "~"
    elif issue:
        symbol = "!"
    else:
        symbol = "✓"

    debug = " DBG" if health.get("debug_mode") else ""
    return f"{avatar}   [{cycle} | {active}/{provisional}p | {mode_label} | {symbol}]{debug}"


def handle_cli_command(runtime: RuntimeRuntime, user_input: str) -> tuple[bool, str | None]:
    if user_input == "/help-tools":
        return True, example_tool_snippet()
    if user_input == "/tools":
        specs = [f"- {spec.name}: {spec.description}" for spec in runtime.registry.list_specs()]
        return True, "\n".join(specs) if specs else "No tools loaded."
    if user_input == "/mode":
        return True, f"Governance mode: {runtime.state.governance.mode}"
    if user_input == "/plan":
        runtime.set_execution_mode(False)
        return True, "Execution mode: plan"
    if user_input in {"/execute", "/execute on"}:
        runtime.set_execution_mode(True)
        return True, "Execution mode: execute"
    if user_input == "/execute off":
        runtime.set_execution_mode(False)
        return True, "Execution mode: plan"
    if user_input.startswith("/mode "):
        mode_val = user_input.split(" ", 1)[1].strip().lower()
        if mode_val == "execute":
            runtime.set_execution_mode(True)
            return True, "Execution mode: execute"
        if mode_val == "plan":
            runtime.set_execution_mode(False)
            return True, "Execution mode: plan"
        try:
            runtime.set_governance_mode(mode_val)
            return True, f"Governance mode: {runtime.state.governance.mode}"
        except ValueError:
            return True, f"Unknown mode: {mode_val}"
    if user_input == "/authority":
        return True, f"Operator authority: {runtime.state.governance.operator_authority.upper()}"
    if user_input.startswith("/authority "):
        auth_val = user_input.split(" ", 1)[1].strip().lower()
        try:
            runtime.set_operator_authority(auth_val)
            suffix = ""
            if auth_val == "builder":
                suffix = "\nNote: The policy_engine will now auto-allow standard execution intents (logging preserved)."
            return True, f"Operator authority updated to: {auth_val.upper()}{suffix}"
        except ValueError:
            return True, f"Unknown authority: {auth_val}"
    if user_input == "/kill":
        runtime.engage_killswitch("operator_cli")
        return True, "Emergency stop engaged. Runtime is halted until `/unkill`."
    if user_input == "/unkill":
        runtime.release_killswitch()
        return True, "Emergency stop released. Runtime remains in plan mode until `/execute`."
    if user_input == "/model":
        descriptor = runtime.model.descriptor()
        lines = [f"Model: {descriptor['model']}", f"Provider: {descriptor['provider']}"]
        if descriptor.get("base_url"):
            lines.append(f"Base URL: {descriptor['base_url']}")
        if hasattr(runtime.model, "active_name"):
            lines.insert(0, f"Active model: {runtime.model.active_name}")
        return True, "\n".join(lines)
    if user_input == "/model list" and hasattr(runtime.model, "names"):
        return True, "Models available: " + ", ".join(runtime.model.names())
    if user_input == "/redteam":
        count = int(runtime.state.health_metrics.get("red_team_count", RED_TEAM_BASELINE))
        return True, (
            f"Red-team count: {count}\n"
            f"Metrics: {runtime.red_team_metrics_path}\n"
            f"Receipts: {runtime.red_team_receipts_path}"
        )
    if user_input in {"/sampling", "/signal"}:
        controls = runtime._derive_signal_generation_controls()
        temperature = float(controls.get("temperature", 0.0))
        emotion_scale = float(controls.get("emotion_scale", 0.0))
        logit_bias = controls.get("logit_bias", {})
        logit_bias_count = len(logit_bias) if isinstance(logit_bias, dict) else 0
        return True, (
            f"Sampling controls:\n"
            f"Temperature: {temperature:.4f}\n"
            f"Emotion scale: {emotion_scale:.4f}\n"
            f"Logit bias tokens: {logit_bias_count}"
        )
    if user_input.startswith("/debug"):
        parts = user_input.split(" ", 1)
        if len(parts) > 1:
            val = parts[1].strip().lower()
            if val == "on":
                runtime.state.debug_mode = True
            elif val == "off":
                runtime.state.debug_mode = False
            else:
                return True, f"Usage: /debug [on|off]"
        else:
            runtime.state.debug_mode = not runtime.state.debug_mode
        
        runtime.state.save(Path(runtime.state_path))
        return True, f"Debug mode: {'ON' if runtime.state.debug_mode else 'OFF'}"
    
    if user_input == "/reset-memory":
        res = runtime.reset_memory()
        return True, f"Sandbox memory and in-memory state have been reset. (Files cleared: {res['files_cleared']})"
    
    if user_input == "/simulation":
        from .simulation import SimulationProcessor
        processor = SimulationProcessor(runtime.log_path)
        try:
            results = processor.run()
            if results:
                summary = "\n".join([f"- {r.trait}: {r.resolved_value}" for r in results])
                return True, f"Simulation cycle complete. Consolidated {len(results)} traits:\n{summary}"
            else:
                return True, "Simulation cycle complete. No new identity proposals to consolidate."
        except Exception as exc:
            return True, f"Simulation cycle failed: {exc}"

    if user_input.startswith("/snapshot"):
        parts = user_input.split(" ", 1)
        label = parts[1].strip() if len(parts) > 1 else f"manual_{int(time())}"
        try:
            snap = runtime.sandbox.snapshot_state(label=label)
            return True, f"Snapshot created: {snap['snapshot_id']}"
        except Exception as exc:
            return True, f"Snapshot failed: {exc}"

    return True, f"Unknown command: {user_input}"


def run_plain_shell(runtime: RuntimeRuntime, *, agent_name: str) -> None:
    if runtime.should_resume():
        for line in runtime.emit_resume():
            print(f"\n{agent_name}> {line}")

    while True:
        health = runtime.get_health_signal()
        mode_label = "gov" if runtime.state.governance.mode == "standard" else "exp"
        status = render_status_line(health, mode_label)

        try:
            user_input = input(f"\n{status}\nYou> ").strip()
        except EOFError:
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            break
        if user_input.startswith("/"):
            _, output = handle_cli_command(runtime, user_input)
            if output:
                print(output)
            continue

        reply = runtime.run_single_cycle(user_input)
        print(f"{agent_name}> {reply}")


def _main(*, force_plain_shell: bool = False) -> int:
    parser = build_parser()
    args = parser.parse_args()
    load_dotenv(Path(args.env_file))
    try:
        validate_output_path(Path(args.state_path), "state path")
        validate_output_path(Path(args.log_path), "log path")
        validate_output_path(Path(args.status_path), "status path")
    except ValueError as exc:
        parser.error(str(exc))
    profile = load_agent_profile(args.profile) if args.profile else None
    try:
        resolved_model_name = resolve_model_name(args.model)
    except ValueError as exc:
        parser.error(str(exc))

    if args.model_config:
        model = load_manager_from_config(args.model_config)
    else:
        model = load_model(
            provider=args.provider,
            model=resolved_model_name,
            base_url=args.base_url,
            api_key_env=args.api_key_env,
        )

    agent_name = profile.name if profile is not None else "Runtime"
    morph_channel = MorphChannel()
    runtime_model = ExpressionTapModel(model, morph_channel) if not force_plain_shell else model
    
    # Split Design: CLI is a thin conversation surface.
    # We use SeedRuntime by default if identity tracking is needed, 
    # but for a general CLI we stick to the base RuntimeRuntime or SeedRuntime based on context.
    from .seed_runtime import SeedRuntime
    runtime_cls = SeedRuntime if profile and profile.mode == "seed" else RuntimeRuntime
    led_driver = None
    openrgb_explicit = "--openrgb" in sys.argv
    if getattr(args, "openrgb", False):
        from .led_bus import LEDBus, TickLEDDriver

        ledbus = LEDBus(host=args.openrgb_host, port=int(args.openrgb_port))
        led_driver = TickLEDDriver(ledbus)
        if openrgb_explicit and not led_driver.enabled:
            reason = getattr(ledbus, "reason_unavailable", "unavailable")
            print(
                "OpenRGB LED driver requested but unavailable "
                f"({reason}). Continuing without LED output."
            )

    runtime = runtime_cls(
        state_path=Path(args.state_path),
        log_path=Path(args.log_path),
        mcp_config_path=Path(args.mcp_config),
        workspace_root=Path.cwd(),
        status_path=Path(args.status_path),
        model=runtime_model,
        profile=profile,
        led_driver=led_driver,
    )
    
    # Disable ConsoleSink output for Phase 3 "Conversation surface only" principle.
    # The Router holds sinks. Router is created inside RuntimeRuntime.__init__.
    # We can silence it by setting emit level or just disabling sink.
    if hasattr(runtime.router, "sinks"):
        from .events import ConsoleSink
        for sink in runtime.router.sinks:
            if isinstance(sink, ConsoleSink):
                sink.enabled = False

    def get_avatar_glyph(health: dict[str, Any]) -> str:
        """Derive the ASCII avatar based on health and signal state."""
        issue = health.get("issue", False)
        drift = health.get("drift", False)
        signal = health.get("signal", {})
        
        # Precedence: Issue > Drift > Surge > Flow > Default
        if issue:
            return "( ! _ ! )"
        if drift:
            return "( •_•~ )"
        
        threat = signal.get("threat", 0.0)
        wonder = signal.get("wonder", 0.0)
        curiosity = signal.get("curiosity", 0.8) # default in signal.py is 0.8
        
        if threat > 0.7:
            return "( >_< )"
        if wonder > 0.7 or curiosity > 0.75:
            return "( •̀ ᴗ •́ )"
            
        return "( •_• )"

    def render_status_line(health: dict[str, Any], mode_label: str) -> str:
        # health = {"cycle": int, "active_nodes": int, "provisional_nodes": int, "drift": bool, "issue": bool}
        cycle = health.get("cycle", 0)
        active = health.get("active_nodes", 0)
        provisional = health.get("provisional_nodes", 0)
        drift = health.get("drift", False)
        issue = health.get("issue", False)
        killswitch = health.get("killswitch_engaged", False)
        
        avatar = get_avatar_glyph(health)
        
        if killswitch:
            symbol = "X"
        elif drift:
            symbol = "~"
        elif issue:
            symbol = "!"
        else:
            symbol = "✓"
            
        debug = " DBG" if health.get("debug_mode") else ""
        return f"{avatar}   [{cycle} | {active}/{provisional}p | {mode_label} | {symbol}]{debug}"

    console_server = maybe_start_console_server(args, runtime=runtime)
    console_url = None
    if console_server is not None:
        host, port = console_server.address
        console_url = f"http://{host}:{port}"
    dashboard = maybe_start_dashboard(args, console_url=console_url)
    dashboard_url = dashboard.url if dashboard is not None else None

    try:
        should_launch_panel = args.panel and sys.stdin.isatty() and sys.stdout.isatty() and panel_is_available()
        if should_launch_panel:
            launch_panel_process(
                workspace_root=runtime.workspace_root,
                log_path=Path(args.log_path),
                status_path=Path(args.status_path),
            )
            print(f"Observer panel launched: log={args.log_path} status={args.status_path}")

        # Runtime-only stack: avoid auto-opening browser surfaces.

        use_rich_shell = (not force_plain_shell) and sys.stdin.isatty() and sys.stdout.isatty()
        if use_rich_shell:
            from .rich_shell import rich_shell_is_available, run_rich_shell

            if rich_shell_is_available():
                run_rich_shell(
                    runtime,
                    agent_name=agent_name,
                    morph_channel=morph_channel,
                    command_handler=lambda command: handle_cli_command(runtime, command),
                    console_url=console_url,
                    dashboard_url=dashboard_url,
                )
            else:
                print("Rich shell unavailable. Falling back to the plain shell.")
                print_banner(runtime, agent_name, console_url=console_url, dashboard_url=dashboard_url)
                run_plain_shell(runtime, agent_name=agent_name)
        else:
            print_banner(runtime, agent_name, console_url=console_url, dashboard_url=dashboard_url)
            run_plain_shell(runtime, agent_name=agent_name)
    finally:
        if dashboard is not None:
            dashboard.stop()
        if console_server is not None:
            console_server.stop()
        runtime.state.save(Path(args.state_path))
        runtime.close()
    return 0


def main() -> int:
    return _main(force_plain_shell=False)


def main_plain() -> int:
    return _main(force_plain_shell=True)


if __name__ == "__main__":
    raise SystemExit(main())
