import json
import tempfile
import unittest
from pathlib import Path

from runtime_core.replay import _apply_event
from runtime_core.state import RuntimeState
from runtime_core.truth_api import TruthAPI


def _write_events(log_path: Path, events: list[dict]) -> None:
    with log_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


class TruthAPIOrganismProjectionTests(unittest.TestCase):
    def test_get_identity_includes_canonical_organism(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            events = [
                {
                    "run_id": "r1",
                    "cycle": 1,
                    "seq": 1,
                    "kind": "user_message",
                    "module": "runtime",
                    "details": {"content": "hello"},
                    "timestamp": 1000.0,
                    "event_id": "e1",
                },
                {
                    "run_id": "r1",
                    "cycle": 1,
                    "seq": 2,
                    "kind": "signal.shift",
                    "module": "identity_engine",
                    "details": {"joy": 0.1},
                    "timestamp": 1001.0,
                    "parent_seq": 1,
                    "event_id": "e2",
                },
            ]
            _write_events(log_path, events)

            payload = TruthAPI(log_path).get_identity()
            organism = payload["organism"]

            self.assertIn("identity", organism)
            self.assertIn("affective_state", organism)
            self.assertIn("stm", organism)
            self.assertIn("trajectory", organism)
            self.assertEqual(organism["stm"]["frames"][0]["content"], "hello")
            self.assertEqual(organism["trajectory"]["window"][0]["cause"], "governance_modulation")

    def test_affective_trajectory_is_bounded_and_causal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            events: list[dict] = []
            for seq in range(1, 41):
                events.append(
                    {
                        "run_id": "r1",
                        "cycle": 1,
                        "seq": seq,
                        "kind": "signal.shift",
                        "module": "runtime",
                        "details": {"curiosity": 0.01},
                        "timestamp": 1000.0 + seq,
                        "parent_seq": seq - 1 if seq > 1 else None,
                        "event_id": f"e{seq}",
                    }
                )
            _write_events(log_path, events)

            organism_payload = TruthAPI(log_path).get_organism(trajectory_window=10)
            window = organism_payload["organism"]["trajectory"]["window"]

            self.assertEqual(len(window), 10)
            self.assertTrue(all(point.get("source_operation") for point in window))
            self.assertTrue(all("cause" in point for point in window))
            self.assertEqual(window[0]["seq"], 31)
            self.assertEqual(window[-1]["seq"], 40)

    def test_replay_live_parity_for_affective_state_and_trajectory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            events = [
                {
                    "run_id": "r1",
                    "cycle": 1,
                    "seq": 1,
                    "kind": "signal.shift",
                    "module": "runtime",
                    "details": {"joy": 0.2},
                    "timestamp": 1000.0,
                    "event_id": "e1",
                },
                {
                    "run_id": "r1",
                    "cycle": 1,
                    "seq": 2,
                    "kind": "signal.decay",
                    "module": "runtime",
                    "details": {"factor": 0.5},
                    "timestamp": 1001.0,
                    "parent_seq": 1,
                    "event_id": "e2",
                },
            ]
            _write_events(log_path, events)

            # Replay-projected organism.
            api_payload = TruthAPI(log_path).get_organism(trajectory_window=8)
            organism = api_payload["organism"]

            # Live-apply the exact same events and compare endpoint affective state.
            live_state = RuntimeState()
            for event in events:
                _apply_event(live_state, event)

            self.assertEqual(organism["affective_state"]["core"]["joy"], live_state.signal.core["joy"])
            self.assertEqual(len(organism["trajectory"]["window"]), 2)
            self.assertEqual(organism["trajectory"]["window"][0]["kind"], "signal.shift")
            self.assertEqual(organism["trajectory"]["window"][1]["kind"], "signal.decay")


if __name__ == "__main__":
    unittest.main()
