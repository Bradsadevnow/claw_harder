from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from time import time
from typing import Any, Dict, List, Optional
from typing import TYPE_CHECKING

try:
    import wasmtime  # type: ignore[import-not-found]
except ImportError:
    wasmtime = None  # type: ignore[assignment]

if TYPE_CHECKING:
    import wasmtime as wasmtime_types

WASM_AVAILABLE = wasmtime is not None


def _require_wasmtime() -> Any:
    if wasmtime is None:
        raise RuntimeError("WASM features not available. Install with the 'wasm' extra.")
    return wasmtime


class ExecutionDomain(Enum):
    NATIVE = "native"
    SANDBOX = "sandbox"


class MonitorClass(Enum):
    NORMAL = "normal"
    REPAIR_LAB = "repair_lab"
    CORONER = "coroner"


@dataclass
class ExecutionEnvelope:
    domain: ExecutionDomain
    monitor: MonitorClass
    preopen_dirs: List[tuple[str, str]] = field(default_factory=list)
    env: List[tuple[str, str]] = field(default_factory=list)
    memory_limit_bytes: Optional[int] = None
    cpu_limit_ms: Optional[int] = None


@dataclass
class VerifiedRepairReceipt:
    patch_hash: str
    verified_at: float = field(default_factory=time)
    outcome: str = "success"
    invariants_passed: List[str] = field(default_factory=list)
    narrative: str = ""
    chamber_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "patch_hash": self.patch_hash,
            "verified_at": self.verified_at,
            "outcome": self.outcome,
            "invariants_passed": self.invariants_passed,
            "chamber_id": self.chamber_id,
        }


class WasmExecutor:
    """A physically constrained execution chamber for Runtime operations."""

    def __init__(self, engine: Optional["wasmtime_types.Engine"] = None):
        runtime_wasmtime = _require_wasmtime()
        # We use a shared engine but unique stores for each execution pulse
        self.engine = engine or runtime_wasmtime.Engine()
        self.linker = runtime_wasmtime.Linker(self.engine)
        self.linker.define_wasi()

    def create_session(self, envelope: ExecutionEnvelope) -> "wasmtime_types.Store":
        """Create a new session (Store) with capabilities mapped from the envelope."""
        runtime_wasmtime = _require_wasmtime()
        config = runtime_wasmtime.WasiConfig()
        
        # Capability Injection: Pre-open directories
        for host_path, guest_path in envelope.preopen_dirs:
            config.preopen_dir(host_path, guest_path)
            
        # Capability Injection: Environment variables
        if envelope.env:
            config.env = envelope.env
            
        # Default security: Inherit NO standard I/O unless explicitly allowed
        # (For now, we keep them isolated; the Repair Lab will use virtual files)
        
        store = runtime_wasmtime.Store(self.engine)
        store.set_wasi(config)
        
        # TODO: Implement memory/CPU limits if supported by current wasmtime-py version
        # envelope.memory_limit_bytes ...
        
        return store

    def execute(
        self, 
        wasm_path: Path, 
        envelope: ExecutionEnvelope, 
        export_name: str = "_start"
    ) -> Dict[str, Any]:
        """Execute a WASM module within the constrained envelope."""
        runtime_wasmtime = _require_wasmtime()
        if not wasm_path.exists():
            raise FileNotFoundError(f"WASM module not found: {wasm_path}")

        module = runtime_wasmtime.Module.from_file(self.engine, str(wasm_path))
        store = self.create_session(envelope)
        instance = self.linker.instantiate(store, module)
        
        func = instance.exports(store).get(export_name)
        if not func or not isinstance(func, runtime_wasmtime.Func):
            raise KeyError(f"Export '{export_name}' not found or not a function in {wasm_path.name}")

        try:
            start_ts = time()
            result = func(store)
            duration = time() - start_ts
            
            return {
                "success": True,
                "result": result,
                "duration_ms": duration * 1000,
                "chamber_id": f"wasm-{int(start_ts)}",
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "chamber_id": "wasm-fail",
            }
