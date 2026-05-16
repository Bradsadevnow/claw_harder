from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

@dataclass
class DomainSnapshot:
    """A domain-specific snapshot of the reasoning state."""
    variables: Dict[str, Any] = field(default_factory=dict)
    violations: List[str] = field(default_factory=list)
    valid_math: bool = True
    raw_data: Dict[str, Any] = field(default_factory=dict)

class DomainAdapter(ABC):
    """
    Abstract base class for all Grounding Adapters.
    """

    @abstractmethod
    def extract_snapshot(self, monologue: str, current_working: Any) -> DomainSnapshot:
        """
        Extract domain-specific variables from the model's monologue.
        """
        pass

    @abstractmethod
    def validate_snapshot(self, snapshot: DomainSnapshot, current_working: Any) -> List[str]:
        """
        Validate the extracted snapshot against the authoritative ground truth.
        Returns a list of violation strings.
        """
        pass

    @abstractmethod
    def get_initial_state(self) -> Dict[str, Any]:
        """
        Return the default state variables for this domain.
        """
        pass

    @property
    @abstractmethod
    def domain_name(self) -> str:
        """
        Return the unique identifier for this domain.
        """
        pass
