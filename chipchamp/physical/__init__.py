"""Physical-implementation intelligence (PPA feedback loop).

Parses what the RTL-to-GDSII vertical produces (OpenROAD/OpenSTA path reports,
gate netlists, yosys liberty-stat) and cross-probes it back to the RTL objects
in the design database — so a post-route timing path or an area hog points at
``file:line``, not at ``_54_/D``.
"""
from .reports import TimingPath, find_run_dir, find_sta_reports, parse_path_report
from .xprobe import GateNetlist, resolve_hier, rtl_net_of

__all__ = [
    "TimingPath",
    "parse_path_report",
    "find_run_dir",
    "find_sta_reports",
    "GateNetlist",
    "rtl_net_of",
    "resolve_hier",
]
