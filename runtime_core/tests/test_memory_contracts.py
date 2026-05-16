import json
import tempfile
import unittest
from pathlib import Path

from runtime_core.memory import (
    ASSOCIATIVE_RECALL_POLICY,
    DeterministicAdmitter,
    MemoryStore,
    RecallCandidate,
    SemanticNominationIndex,
)
from runtime_core.state import RuntimeState


class MemoryContractTests(unittest.TestCase):
    def test_stm_epoch_atomic_eviction(self) -> None:
        memory = MemoryStore()

        # 8 complete epochs; STM keeps only last 6 via whole-epoch eviction.
        for i in range(8):
            memory.append("user", f"u{i}")
            memory.append("assistant", f"a{i}")

        epochs = memory.stm.recent_epochs(include_open=False)
        self.assertEqual(len(epochs), 6)
        self.assertTrue(all(len(epoch.messages) == 2 for epoch in epochs))
        self.assertEqual(epochs[0].messages[0].content, "u2")
        self.assertEqual(epochs[-1].messages[-1].content, "a7")

    def test_mtm_is_append_only_even_when_stm_evicts(self) -> None:
        memory = MemoryStore()
        for i in range(8):
            memory.append("user", f"u{i}")
            memory.append("assistant", f"a{i}")

        # All 16 messages remain in MTM, plus epoch event rows.
        message_rows = [row for row in memory.mtm.all() if row.event_kind == "message.appended"]
        self.assertEqual(len(message_rows), 16)
        self.assertEqual(message_rows[0].content, "u0")
        self.assertEqual(message_rows[-1].content, "a7")
        self.assertEqual([row.seq for row in memory.mtm.all()], sorted(row.seq for row in memory.mtm.all()))

    def test_lifecycle_audit_events_cover_epoch_creation_eviction_and_close(self) -> None:
        memory = MemoryStore()
        for i in range(8):
            memory.append("user", f"u{i}")
            memory.append("assistant", f"a{i}")

        kinds = [entry.event_kind for entry in memory.mtm.all()]
        self.assertEqual(kinds.count("epoch_created"), 8)
        self.assertEqual(kinds.count("epoch_closed"), 8)
        self.assertEqual(kinds.count("epoch_evicted"), 2)

    def test_session_close_emits_ltm_artifact_written_event(self) -> None:
        memory = MemoryStore()
        memory.append("user", "Need deployment plan")
        memory.append("assistant", "Drafting plan")

        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = memory.close_session(session_id="main", ltm_root=Path(tmpdir))
            kinds = [entry.event_kind for entry in memory.mtm.all()]
            self.assertIn("session_closed", kinds)
            self.assertIn("compression_started", kinds)
            self.assertIn("ltm_artifact_written", kinds)
            self.assertIn("compression_completed", kinds)
            self.assertIn("ltm_promoted", kinds)
            self.assertTrue(artifact.artifact_id.startswith("LTM_main_"))

    def test_ltm_compression_writes_artifact_and_toc(self) -> None:
        memory = MemoryStore()
        memory.append("user", "Need deployment plan")
        memory.append("assistant", "Drafting plan")
        memory.append("user", "Include rollback")

        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = memory.close_session(session_id="main", ltm_root=Path(tmpdir))
            self.assertTrue(artifact.artifact_id.startswith("LTM_main_"))

            ltm_path = Path(tmpdir) / "ltm.jsonl"
            toc_path = Path(tmpdir) / "ltm_toc.json"
            self.assertTrue(ltm_path.exists())
            self.assertTrue(toc_path.exists())

            lines = [line for line in ltm_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertEqual(payload["artifact_id"], artifact.artifact_id)
            self.assertEqual(payload["session_id"], "main")

            toc = json.loads(toc_path.read_text(encoding="utf-8"))
            self.assertEqual(toc["version"], "ltm-toc-v1")
            self.assertEqual(len(toc["episodes"]), 1)
            self.assertEqual(toc["episodes"][0]["artifact_id"], artifact.artifact_id)

    def test_semantic_nomination_is_non_authoritative(self) -> None:
        self.assertEqual(ASSOCIATIVE_RECALL_POLICY, "Associative Recall Is Non-Authoritative")
        candidate = RecallCandidate(
            candidate_id="semantic:abc",
            source="semantic",
            content="Possible related episode",
            reference="abc",
            score=0.9,
        )
        decision = DeterministicAdmitter().evaluate(candidate)
        self.assertEqual(decision.decision, "reject")
        self.assertEqual(decision.reason_code, "semantic_nomination_requires_governance_admission")

    def test_semantic_nomination_surface_is_optional(self) -> None:
        episodes = [
            {"artifact_id": "ep1", "session_id": "s1", "summary": "Recursive governance and runtime admissibility"},
            {"artifact_id": "ep2", "session_id": "s2", "summary": "Unrelated gardening notes"},
        ]
        disabled = SemanticNominationIndex(enabled=False)
        self.assertEqual(disabled.nominate("governance", episodes=episodes), [])

        enabled = SemanticNominationIndex(enabled=True)
        nominations = enabled.nominate("governance admissibility", episodes=episodes, limit=3)
        self.assertEqual(len(nominations), 1)
        self.assertEqual(nominations[0].reference, "ep1")

    def test_nomination_and_admission_emit_audit_events(self) -> None:
        memory = MemoryStore()
        episodes = [
            {"artifact_id": "ep1", "session_id": "s1", "summary": "Recursive governance and runtime admissibility"},
        ]

        nominations = memory.semantic_nominate(
            "governance admissibility",
            episodes=episodes,
            limit=3,
            index=SemanticNominationIndex(enabled=True),
        )
        self.assertEqual(len(nominations), 1)

        generated = [entry for entry in memory.mtm.all() if entry.event_kind == "semantic_nomination_generated"]
        self.assertEqual(len(generated), 1)
        self.assertEqual(generated[0].metadata.get("count"), 1)

        semantic_decision = memory.admit_candidate(nominations[0])
        self.assertEqual(semantic_decision.decision, "reject")

        deterministic_candidate = RecallCandidate(
            candidate_id="mtm:1",
            source="mtm",
            content="deterministic",
            reference="seq:1",
        )
        deterministic_decision = memory.admit_candidate(deterministic_candidate)
        self.assertEqual(deterministic_decision.decision, "admit")

        kinds = [entry.event_kind for entry in memory.mtm.all()]
        self.assertIn("nomination_rejected", kinds)
        self.assertIn("context_admitted", kinds)

    def test_state_roundtrip_persists_memory_contract_structures(self) -> None:
        state = RuntimeState()
        state.memory.append("user", "hello")
        state.memory.append("assistant", "hey")

        with tempfile.TemporaryDirectory() as tmpdir:
            state.memory.close_session("main", Path(tmpdir) / "ltm")
            payload = state.to_persisted_dict()
            payload.pop("vitals", None)
            restored = RuntimeState.from_dict(payload)

            self.assertGreaterEqual(len(restored.memory.stm.epochs), 1)
            self.assertGreaterEqual(len(restored.memory.mtm.entries), 1)
            self.assertIsNotNone(restored.memory.ltm_last_artifact)
            self.assertGreaterEqual(len(restored.memory.ltm_toc_tail), 1)


if __name__ == "__main__":
    unittest.main()
