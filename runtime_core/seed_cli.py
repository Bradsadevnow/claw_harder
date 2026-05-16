from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .agent_profile import load_agent_profile
from .cli import load_dotenv
from .model import load_model, normalize_message_text, ModelResponse
from .seed_runtime import (
    SeedRuntime, SeedCandidate, DASHBOARD_ORDER, 
    _require_memory_type, _format_timestamp, finalize_response_text
)


class SeedApp:
    def __init__(
        self,
        runtime: SeedRuntime,
        data_dir: Path,
    ) -> None:
        self.runtime = runtime
        self.data_dir = data_dir
        self.profile = runtime.profile
        self.user_message_count = 0

    def run(self) -> int:
        self._print_banner()
        self._ensure_calibration()
        while True:
            try:
                user_input = input("\nYou> ").strip()
            except EOFError:
                print()
                break

            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit"}:
                break
            if user_input.startswith("/"):
                if self._handle_command(user_input):
                    break
                continue

            reply = self._handle_user_message(user_input)
            print(f"\n{self.profile.name}> {reply}")
        return 0

    def _print_banner(self) -> None:
        descriptor = self.runtime.model.descriptor()
        print(f"{self.profile.name} terminal loop (SeedRuntime)")
        print("Type `exit` to stop.")
        print("Commands: `/help`, `/dashboard`, `/pending`, `/edit <type> <index> <text>`, `/delete <type> <index>`")
        print(f"Model: {descriptor['model']} [{descriptor['provider']}]")
        if descriptor.get("base_url"):
            print(f"Base URL: {descriptor['base_url']}")

    def _ensure_calibration(self) -> None:
        understand = self.runtime.state.identity.calibrations.get("understand_or_improve")
        if not understand:
            print(f"\n{self.profile.name}> I'm here to help you understand yourself over time. I'll suggest patterns I notice, and you can correct me anytime.")
            answer = input("Calibration> What are you trying to understand or improve about yourself? ").strip()
            if answer:
                self.runtime.emit_calibration_updated("understand_or_improve", answer)

        attention = self.runtime.state.identity.calibrations.get("pay_attention_to")
        if not attention:
            answer = input("Calibration> What should I pay attention to when you talk? ").strip()
            if answer:
                self.runtime.emit_calibration_updated("pay_attention_to", answer)

    def _handle_command(self, user_input: str) -> bool:
        if user_input == "/help":
            print("`/dashboard` shows confirmed memory.")
            print("`/pending` shows the current queued confirmation.")
            print("`/edit <type> <index> <text>` edits a confirmed memory.")
            print("`/delete <type> <index>` deletes a confirmed memory.")
            print("`/quit` or `exit` stops the loop.")
            return False
        if user_input == "/dashboard":
            self._print_dashboard()
            return False
        if user_input == "/pending":
            self._show_pending_candidate()
            return False
        if user_input.startswith("/edit "):
            self._handle_edit_command(user_input)
            return False
        if user_input.startswith("/delete "):
            self._handle_delete_command(user_input)
            return False
        if user_input in {"/quit", "/exit"}:
            return True
        print("Unknown command. Type `/help`.")
        return False

    def _handle_edit_command(self, user_input: str) -> None:
        parts = user_input.split(maxsplit=3)
        if len(parts) < 4:
            print("Usage: /edit <type> <index> <text>")
            return
        memory_type = parts[1].strip().lower()
        try:
            _require_memory_type(memory_type)
            index = int(parts[2])
            bucket = self.runtime.projected_identity_bucket(memory_type)
            if index < 1 or index > len(bucket):
                raise IndexError(f"{memory_type} index out of range")
            target = bucket[index - 1]
            item = self.runtime.emit_identity_edited(memory_type, target["id"], parts[3])
        except (ValueError, IndexError) as exc:
            print(f"Edit failed: {exc}")
            return
        print(f"Updated {memory_type} #{index}: {item['content']}")

    def _handle_delete_command(self, user_input: str) -> None:
        parts = user_input.split(maxsplit=2)
        if len(parts) != 3:
            print("Usage: /delete <type> <index>")
            return
        memory_type = parts[1].strip().lower()
        try:
            _require_memory_type(memory_type)
            index = int(parts[2])
            bucket = self.runtime.projected_identity_bucket(memory_type)
            if index < 1 or index > len(bucket):
                raise IndexError(f"{memory_type} index out of range")
            target = bucket[index - 1]
            item = self.runtime.emit_identity_deleted(memory_type, target["id"])
        except (ValueError, IndexError) as exc:
            print(f"Delete failed: {exc}")
            return
        print(f"Deleted {memory_type} #{index}: {item.get('content', item['id'])}")

    def _handle_user_message(self, user_input: str) -> str:
        self.user_message_count += 1
        return self.runtime.run(user_input)

    def _show_pending_candidate(self) -> None:
        active = self.runtime.active_confirmation
        if active is not None:
            print("Pending confirmation")
            print(f"- type: {active.type}")
            print(f"- content: {active.content}")
            print(f"- confidence: {active.confidence:.2f}")
            if len(self.runtime.pending_candidates) > 0:
                print(f"- queued behind active: {len(self.runtime.pending_candidates)}")
            return

        if not self.runtime.pending_candidates:
            print("No pending confirmations.")
            return
        candidate = self.runtime.pending_candidates[0]
        print("Pending confirmation")
        print(f"- type: {candidate.type}")
        print(f"- content: {candidate.content}")
        print(f"- confidence: {candidate.confidence:.2f}")
        if len(self.runtime.pending_candidates) > 1:
            print(f"- queued behind active: {len(self.runtime.pending_candidates) - 1}")

    def _print_dashboard(self) -> None:
        all_memories = self.runtime.projected_identity_buckets()
        print("\nDashboard")
        for memory_type in DASHBOARD_ORDER:
            label = memory_type.capitalize()
            items = all_memories.get(memory_type, [])
            print(f"{label} ({len(items)})")
            if not items:
                print("  - none")
                continue
            for index, item in enumerate(items, start=1):
                timestamp = _format_timestamp(item.get("updated_at") or item.get("confirmed_at"))
                confidence = float(item.get("confidence", 0.0))
                content = str(item.get("content", "")).strip()
                print(f"  {index}. {content} [{confidence:.2f}] {timestamp}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Runtime Seed terminal loop")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--provider", default="lmstudio", choices=["auto", "demo", "xai", "openai", "openai-compatible", "lmstudio"])
    parser.add_argument("--model", default="openai/gpt-oss-20b")
    parser.add_argument("--base-url", default="http://192.168.1.129:1234/v1")
    parser.add_argument("--api-key-env")
    parser.add_argument("--profile", default="research/runtime_seed_v1/agent_profile.json")
    parser.add_argument("--data-dir", default=".runtime_seed")
    parser.add_argument("--session-id", default="default")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    load_dotenv(Path(args.env_file))
    profile = load_agent_profile(args.profile)
    model = load_model(
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
    )
    data_dir = Path(args.data_dir)
    state_path = data_dir / "sandbox" / args.session_id / "state.json"
    log_path = data_dir / "sandbox" / args.session_id / "events.jsonl"
    mcp_config_path = data_dir / "mcp_servers.json"
    
    runtime = SeedRuntime(
        state_path=state_path,
        log_path=log_path,
        mcp_config_path=mcp_config_path,
        workspace_root=Path.cwd(),
        model=model,
        profile=profile,
    )
    
    app = SeedApp(
        runtime=runtime,
        data_dir=data_dir,
    )
    return app.run()


if __name__ == "__main__":
    import sys
    sys.exit(main())
