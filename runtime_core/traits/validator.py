from __future__ import annotations
from typing import Any

# Centralized Identity Trait Configuration
# This governs how identity evolution is resolved during the Simulation Phase.
IDENTITY_TRAIT_CONFIG = {
    "name": {"mutable": False, "merge": False},
    "purpose": {"mutable": True, "merge": False},
    "core_directive": {"mutable": True, "merge": False},
    "voice_style": {"mutable": True, "merge": True},
    "voice_avoid": {"mutable": True, "merge": True},
    "values": {"mutable": True, "merge": True},
    "constraints": {"mutable": True, "merge": True},
    "reinforced_symbols": {"mutable": True, "merge": True},
    "loop_count": {"mutable": True, "merge": False},
}

# Exported for components like state.py
IDENTITY_TRAITS = list(IDENTITY_TRAIT_CONFIG.keys())

def validate_identity_trait(trait: str) -> bool:
    """Returns True if the trait is part of the locked identity schema."""
    return trait in IDENTITY_TRAIT_CONFIG

def normalize_identity_value(trait: str, value: Any) -> Any:
    """Canonicalize identity values based on trait type."""
    if trait in {"values", "voice_style", "voice_avoid", "constraints"}:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return []
    
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()

def merge_identity_trait(trait: str, existing: Any, new: Any) -> Any:
    """
    Implements merge semantics for identity traits.
    - Lists: Appends new unique values to existing.
    - Others: Overwrites if mutable, otherwise returns existing.
    """
    config = IDENTITY_TRAIT_CONFIG.get(trait, {})
    
    # 1. Handle Mergeable Traits (Lists)
    if config.get("merge"):
        existing_list = normalize_identity_value(trait, existing)
        new_list = normalize_identity_value(trait, new)
        
        # Unique merge
        combined = list(existing_list)
        for val in new_list:
            if val not in combined:
                combined.append(val)
        return combined

    # 2. Handle Mutable Traits
    if config.get("mutable", True):
        return normalize_identity_value(trait, new)
        
    return existing
