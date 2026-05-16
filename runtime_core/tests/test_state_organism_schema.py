import unittest

from runtime_core.state import RuntimeState


class RuntimeStateOrganismSchemaTests(unittest.TestCase):
    def test_to_persisted_dict_includes_canonical_organism_block(self) -> None:
        state = RuntimeState()
        state.identity.name = "Halcyon"
        state.identity.mode = "dude"
        state.signal.valence = 0.91
        state.signal.arousal = 0.42
        state.memory.append("user", "hello")

        payload = state.to_persisted_dict()

        self.assertIn("organism", payload)
        self.assertEqual(payload["organism"]["identity"]["name"], "Halcyon")
        self.assertEqual(payload["organism"]["identity"]["mode"], "dude")
        self.assertEqual(payload["organism"]["affective_state"]["valence"], 0.91)
        self.assertEqual(payload["organism"]["affective_state"]["arousal"], 0.42)
        self.assertEqual(payload["organism"]["stm"]["frames"][0]["content"], "hello")
        # Compatibility mirrors remain available for existing callers.
        self.assertEqual(payload["identity"]["name"], "Halcyon")

    def test_from_dict_prefers_organism_when_both_are_present(self) -> None:
        payload = {
            "schema_version": 5,
            "identity": {"name": "legacy-name", "mode": "legacy-mode"},
            "signal": {"valence": 0.1, "arousal": 0.1, "instability": 0.1, "core": {}, "trace": []},
            "memory": {"frames": [{"role": "user", "content": "legacy-frame", "ts": 1.0}], "notes": [], "semantic_items": []},
            "organism": {
                "identity": {"name": "organism-name", "mode": "organism-mode"},
                "affective_state": {"valence": 0.8, "arousal": 0.7, "instability": 0.2, "core": {}, "trace": []},
                "stm": {"frames": [{"role": "user", "content": "organism-frame", "ts": 2.0}], "notes": [], "semantic_items": []},
            },
        }

        state = RuntimeState.from_dict(payload)

        self.assertEqual(state.identity.name, "organism-name")
        self.assertEqual(state.identity.mode, "organism-mode")
        self.assertEqual(state.signal.valence, 0.8)
        self.assertEqual(state.signal.arousal, 0.7)
        self.assertEqual(state.memory.frames[0].content, "organism-frame")

    def test_from_dict_falls_back_to_legacy_shape(self) -> None:
        payload = {
            "schema_version": 5,
            "identity": {"name": "legacy-name", "mode": "legacy-mode"},
            "signal": {"valence": 0.55, "arousal": 0.25, "instability": 0.05, "core": {}, "trace": []},
            "memory": {"frames": [{"role": "assistant", "content": "legacy-frame", "ts": 3.0}], "notes": [], "semantic_items": []},
        }

        state = RuntimeState.from_dict(payload)

        self.assertEqual(state.identity.name, "legacy-name")
        self.assertEqual(state.identity.mode, "legacy-mode")
        self.assertEqual(state.signal.valence, 0.55)
        self.assertEqual(state.signal.arousal, 0.25)
        self.assertEqual(state.memory.frames[0].content, "legacy-frame")


if __name__ == "__main__":
    unittest.main()
