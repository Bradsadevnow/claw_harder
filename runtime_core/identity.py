from __future__ import annotations
from .traits.validator import (
    IDENTITY_TRAIT_CONFIG,
    IDENTITY_TRAITS,
    validate_identity_trait,
    normalize_identity_value,
    merge_identity_trait
)

# This file now acts as the 'State Authority' for identity, 
# while delegating validation logic to traits/validator.py.
