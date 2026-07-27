"""Data model for the design database (SPEC §8.3).

These dataclasses are the structured semantic facts the agent queries instead of
grepping raw source. They are intentionally close to what a synthesizable subset
of SystemVerilog exposes: modules, ports, params, instances, procedural blocks,
and continuous assignments — enough to elaborate a hierarchy, infer clock/reset
domains, extract FSMs and slice fan-in cones.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Port:
    name: str
    direction: str  # input | output | inout | interface
    dtype: str = "wire"  # logic | wire | reg | bit | <typedef> | <interface>
    signed: bool = False
    msb: Optional[str] = None  # packed range MSB expression, e.g. "WIDTH-1"
    lsb: Optional[str] = None
    width_expr: Optional[str] = None  # human-readable width, e.g. "[WIDTH-1:0]"
    file: str = ""
    line: int = 0

    @property
    def is_vector(self) -> bool:
        return self.msb is not None


@dataclass
class Parameter:
    name: str
    default_expr: str = ""
    dtype: str = ""
    is_localparam: bool = False
    file: str = ""
    line: int = 0


@dataclass
class Instance:
    inst_name: str
    module_type: str
    param_overrides: dict[str, str] = field(default_factory=dict)
    connections: dict[str, str] = field(default_factory=dict)  # port -> connected expr
    ordered: bool = False  # positional (ordered) connection
    file: str = ""
    line: int = 0


@dataclass
class AlwaysBlock:
    kind: str  # always_ff | always_comb | always_latch | always
    sens: list[tuple[str, str]] = field(default_factory=list)  # (edge, signal)
    clock: Optional[str] = None
    resets: list[str] = field(default_factory=list)
    lhs: set[str] = field(default_factory=set)  # signals assigned (base names)
    rhs: set[str] = field(default_factory=set)  # signals read (base names)
    body: str = ""
    file: str = ""
    line: int = 0


@dataclass
class ContinuousAssign:
    lhs: str
    rhs: str
    lhs_signals: set[str] = field(default_factory=set)
    rhs_signals: set[str] = field(default_factory=set)
    file: str = ""
    line: int = 0


@dataclass
class Net:
    name: str
    dtype: str = "wire"
    signed: bool = False
    msb: Optional[str] = None
    lsb: Optional[str] = None
    file: str = ""
    line: int = 0


@dataclass
class EnumDef:
    name: str  # typedef name (may be "")
    base: str = ""
    members: dict[str, str] = field(default_factory=dict)  # member -> value expr
    file: str = ""
    line: int = 0


@dataclass
class Module:
    name: str
    file: str = ""
    line: int = 0
    line_end: int = 0
    is_ansi: bool = True
    params: list[Parameter] = field(default_factory=list)
    ports: list[Port] = field(default_factory=list)
    instances: list[Instance] = field(default_factory=list)
    always_blocks: list[AlwaysBlock] = field(default_factory=list)
    assigns: list[ContinuousAssign] = field(default_factory=list)
    nets: list[Net] = field(default_factory=list)
    enums: list[EnumDef] = field(default_factory=list)
    parse_confidence: str = "high"  # high | degraded (preprocessor-heavy / recovered)

    def port(self, name: str) -> Optional[Port]:
        return next((p for p in self.ports if p.name == name), None)

    def param(self, name: str) -> Optional[Parameter]:
        return next((p for p in self.params if p.name == name), None)


@dataclass
class FSM:
    module: str
    state_reg: str
    next_reg: Optional[str]
    states: list[str] = field(default_factory=list)
    transitions: list[tuple[str, str, str]] = field(default_factory=list)  # (from, to, cond)
    encoding: str = "unknown"  # binary | onehot | gray | unknown
    file: str = ""
    line: int = 0


# ---- elaborated hierarchy ---------------------------------------------------


@dataclass
class HierNode:
    inst_name: str  # leaf instance name ("" for the top)
    module: str  # module type
    path: str  # full hierarchical path, e.g. tb_top.dut.u_wr
    params: dict[str, str] = field(default_factory=dict)  # resolved parameter values
    children: list["HierNode"] = field(default_factory=list)
    unresolved: bool = False  # module type not found in the DB
    note: str = ""
