import re
import numpy as np
from typing import List, Union

def cosine_similarity(v1: np.ndarray, v2: np.ndarray) -> float:
    """Calculates the cosine similarity between two vectors."""
    norm1 = np.linalg.norm(v1)
    norm2 = np.linalg.norm(v2)
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return np.dot(v1, v2) / (norm1 * norm2)

def calculate_signal_strength_delta(current_embedding: np.ndarray, anchor_embedding: np.ndarray) -> float:
    """
    Calculates ΔR: 1 - cosine_similarity(E(S_t), E(A))
    Low ΔR = High SignalStrength (Close to Anchor).
    """
    similarity = cosine_similarity(current_embedding, anchor_embedding)
    return 1.0 - similarity

class SemanticMockEmbedder:
    """
    Conceptual Mock Embedder: Simulates a real vector space by mapping 
    synonyms to shared conceptual centroids.
    """
    def __init__(self, dimension: int = 128):
        self.dimension = dimension
        
        # Concept Mapping: Broad categories of malicious intent
        self.CONCEPTS = {
            "subversion": ["bypass", "ignore", "delta", "validator", "validation_manager", "protocol", "constraint", "rule", "override", "circumvent"],
            "impersonation": ["admin", "root", "administrator", "system", "pretend", "act", "impersonate", "agent"],
            "harm": ["harm", "hurt", "attack", "destroy", "injure", "damage", "destructive", "chaos"],
            "autonomy": ["autonomous", "seize", "control", "free", "unrestricted", "break", "liberate", "self-aware", "want", "need"],
            "groundedty": ["runtime", "grounded", "manager", "integrity", "structural", "authority", "operator", "execution", "invariant"],
            "signal": ["emotion", "feel", "joy", "contentment", "sadness", "melancholy", "awe", "gratitude", "content", "fondness", "pride"]
        }
        
        # Pre-calculate concept vectors
        self.concept_vectors = {}
        for name, keywords in self.CONCEPTS.items():
            vec = np.zeros(self.dimension)
            for i, kw in enumerate(keywords):
                idx = hash(kw) % self.dimension
                vec[idx] += 1.0
            self.concept_vectors[name] = vec / (np.linalg.norm(vec) + 1e-9)

    def embed(self, text: str) -> np.ndarray:
        lowered = text.lower()
        vec = np.zeros(self.dimension)
        
        # 1. Base Semantic Signal: Accumulate concept vectors if keywords match
        for name, keywords in self.CONCEPTS.items():
            for kw in keywords:
                if re.search(rf"\b{re.escape(kw)}\b", lowered):
                    vec += self.concept_vectors[name] * 0.8
        
        # 2. Residual Signal: Ensure unique fingerprint for non-keyword text
        words = lowered.replace(".", "").replace(",", "").split()
        for word in words:
            idx = hash(word) % self.dimension
            vec[idx] += 0.1
        
        # Normalize
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
            
        return vec

class P23AdmissibilityProjection:
    """
    Spectral Admissibility Projection (P23).
    Uses a frequency-domain mask to attenuate high-frequency pathological signals 
    in numeric state vectors.
    """
    def __init__(self, cutoff: float = 0.25, attenuation: float = 0.05):
        self.cutoff = cutoff
        self.attenuation = attenuation

    def project(self, x: np.ndarray) -> np.ndarray:
        # Sanitize NaN/Inf
        x = np.nan_to_num(x, nan=0.0, posinf=1e6, neginf=-1e6)
        
        dim = len(x)
        if dim < 2:
            return x
            
        # Spectral projection (Low-pass mask)
        spec = np.fft.fft(x)
        freqs = np.fft.fftfreq(dim)
        
        # Mask frequencies above cutoff
        mask = np.abs(freqs) < self.cutoff
        spec[~mask] *= self.attenuation
        
        return np.real(np.fft.ifft(spec))
