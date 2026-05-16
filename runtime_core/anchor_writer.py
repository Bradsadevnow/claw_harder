from pathlib import Path
from typing import Optional
from runtime_core.replay import Replayer
from runtime_core.identity import render_identity_for_prompt

class AnchorWriter:
    """
    The 'Projector'.
    Replays the Event Log to generate a human-readable, LLM-friendly anchor.md.
    This is a READ-ONLY PROJECTION of the confirmed identity.
    """

    def __init__(self, log_path: Path, anchor_path: Path):
        self.log_path = log_path
        self.anchor_path = anchor_path
        self.replayer = Replayer()

    def write_anchor(self):
        """
        Replays the log, extracts the confirmed identity, and writes anchor.md.
        """
        # 1. Replay the truth
        state = self.replayer.replay(self.log_path)
        
        # 2. Extract traits from memory buckets
        # IdentityState.memory_buckets: dict[str, list[dict[str, Any]]]
        identity_map = {}
        for trait, bucket in state.identity.memory_buckets.items():
            if bucket:
                # Latest confirmed value is in the bucket node
                identity_map[trait] = bucket[0].get("content", "")

        # 3. Render to Markdown
        # render_identity_for_prompt handles names, purpose, voice, etc.
        rendered = render_identity_for_prompt(identity_map)
        
        # 4. Add Metadata Header
        content = f"""# Runtime Projected Identity
> [!NOTE]
> This file is a projection of confirmed identity events. 
> Source of Truth: {self.log_path.name}
> Last Updated: {state.tick_count} cycles

{rendered}
"""
        
        # 5. Atomic Write
        self.anchor_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.anchor_path.with_suffix(".tmp")
        temp_path.write_text(content.strip(), encoding="utf-8")
        temp_path.replace(self.anchor_path)
