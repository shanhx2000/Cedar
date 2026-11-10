# SPDX-License-Identifier: Apache-2.0
"""Schemas for the three configuration layers.

1. Tool profile      (``cedar/tool-profile@1``)  — user-local executables, environment
                                                   setup, timeouts, toolchain selection.
2. Technology profile (``cedar/technology@1``)   — corners, libraries, LEF/RC, P&R
                                                   parameters, Tcl hooks.
3. Design spec       (``cedar/design@1``)        — RTL, top, clocks (explicit timing goal),
                                                   resets, activity, verification, objective,
                                                   measurement and search settings.
"""

from __future__ import annotations

from .schema import F

ADAPTERS = {
    "synthesis": ("synopsys-dc", "external-ppa"),
    "place_route": ("cadence-innovus",),
    "simulation": ("synopsys-vcs",),
    "saif": ("synopsys-vcd2saif",),
    "power": ("synopsys-primetime",),
    "equivalence": ("cadence-conformal", "external-check"),
}


def _tool(stage: str, doc: str) -> F:
    return F("obj", doc=doc, fields={
        "adapter": F("enum", required=True, choices=ADAPTERS[stage], doc="named adapter implementing this stage"),
        "executable": F("str", required=True, doc="command name or absolute path"),
        "args": F("list", item=F("str"), default=[], doc="extra fixed arguments"),
        "timeout_s": F("int", min=1, default=3600, unit="s", doc="per-invocation timeout"),
        "env_required": F("list", item=F("str", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"), default=[],
                          doc="environment variable NAMES that must be set (values are never recorded)"),
    })


TOOL_PROFILE = {
    "schema": F("enum", required=True, choices=("cedar/tool-profile@1",)),
    "name": F("str", required=True, doc="profile name"),
    "description": F("str", default=""),
    "environment": F("obj", doc="how to prepare the shell for EDA tools", fields={
        "setup_script": F("path", doc="bash file sourced before every tool invocation (site modules, licenses)"),
        "env_required": F("list", item=F("str", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"), default=[],
                          doc="variable names required for all tools"),
        "path_prepend": F("list", item=F("path"), default=[], doc="directories prepended to PATH"),
    }),
    "execution": F("obj", fields={
        "nice": F("int", min=0, max=19, default=10, doc="niceness for EDA processes"),
        "threads": F("int", min=1, max=256, default=4, doc="max threads per EDA tool"),
        "max_parallel": F("int", min=1, max=64, default=2,
                          doc="max candidates measured concurrently (each holds its own tool licenses)"),
        "scratch_dir": F("path", must_exist=False, doc="default work dir root for runs"),
    }),
    "tools": F("obj", required=True, fields={
        "synthesis": _tool("synthesis", "logic synthesis (DC)"),
        "place_route": _tool("place_route", "placement and routing (Innovus)"),
        "simulation": _tool("simulation", "RTL / gate-level simulation for activity and optional checking (VCS)"),
        "saif": _tool("saif", "VCD -> SAIF converter"),
        "power": _tool("power", "activity-based power analysis (PrimeTime)"),
        "equivalence": _tool("equivalence", "RTL equivalence checking (Conformal LEC)"),
    }),
    "toolchain": F("obj", doc="explicit Cedar compiler toolchain selection", fields={
        "prefix": F("path", doc="install prefix with bin/ and share/cedar/ (native build); omit to use the cedar-toolchain wheel"),
    }),
}

_CORNER = F("obj", fields={
    "db": F("list", default=[], item=F("path", glob=True), doc="Synopsys .db libraries (synthesis/power; commercial adapters)"),
    "liberty": F("list", required=True, item=F("path", glob=True), doc="Liberty .lib files (P&R timing)"),
    "sim_models": F("list", default=[], item=F("path", glob=True), doc="Verilog cell models (gate-level simulation; commercial adapters)"),
    "rc_tech": F("path", doc="QRC/RC technology file for extraction (commercial adapters)"),
})

TECHNOLOGY = {
    "schema": F("enum", required=True, choices=("cedar/technology@1",)),
    "name": F("str", required=True),
    "description": F("str", default=""),
    "units": F("obj", required=True, fields={
        "time": F("enum", required=True, choices=("ps", "ns"), doc="library time unit (SDC values and timing reports)"),
        "area": F("enum", default="um2", choices=("um2",)),
    }),
    "corners": F("map", required=True, item=_CORNER, doc="named PVT corners"),
    "default_corner": F("str", required=True),
    "lef": F("obj", doc="required by the commercial P&R adapter", fields={
        "tech": F("path", required=True),
        "cells": F("list", required=True, item=F("path", glob=True)),
    }),
    "synthesis": F("obj", fields={
        "compile_command": F("str", default="compile -map_effort high -area_effort high -no_design_rule -power_effort high"),
        "max_fanout": F("int", min=1, default=16),
        "max_transition": F("float", min=0, unit="library time unit", doc="default 0.050 ns, converted"),
        "input_transition_max": F("float", min=0, unit="library time unit", doc="default 0.060 ns, converted"),
        "input_transition_min": F("float", min=0, unit="library time unit", doc="default 0.020 ns, converted"),
        "dont_use": F("list", item=F("str"), default=[]),
        "pre_compile_hook": F("path", doc="Tcl sourced before compile"),
        "post_compile_hook": F("path", doc="Tcl sourced after compile"),
    }),
    "place_route": F("obj", doc="required by the commercial P&R adapter", fields={
        "site": F("str", required=True),
        "core_utilization": F("float", min=0.05, max=1.0, default=0.6),
        "core_margin": F("float", min=0, default=4.0, unit="um"),
        "power_net": F("str", default="VDD"),
        "ground_net": F("str", default="VSS"),
        "filler_cells": F("list", item=F("str"), default=[]),
        "max_route_layer": F("int", min=1, default=6),
        "route_top_layer": F("int", min=1, default=5),
        "route_bottom_layer": F("int", min=1, default=2),
        "power_rail_layer_range": F("list", item=F("str"), default=["M1", "M3"]),
        "post_init_hook": F("path", doc="Tcl sourced after init_design"),
        "pre_route_hook": F("path", doc="Tcl sourced before routeDesign"),
        "accepted_errors": F("list", item=F("str"), default=[],
                             doc="Innovus message IDs (e.g. IMPTR-2101) known to be harmless for this technology; "
                                 "accepted only during floorplan/power-route/place/route/filler/hook steps and "
                                 "recorded with the stage result. Any other Innovus error fails P&R."),
    }),
    "equivalence": F("obj", fields={
        "extra_commands": F("list", item=F("str"), default=[], doc="Conformal dofile commands inserted before 'set system mode lec'"),
    }),
}

CLOCK = F("obj", fields={
    "port": F("str", doc="clock port of the top module; omit for a virtual clock (combinational designs)"),
    "name": F("str", default="clk"),
    "period_ns": F("float", min=1e-4, unit="ns", doc="timing goal as a clock period"),
    "frequency": F("str", doc="alternatively a unit-qualified frequency, e.g. '1.25 GHz'"),
})

DESIGN = {
    "schema": F("enum", required=True, choices=("cedar/design@1",)),
    "name": F("str", required=True),
    "top": F("str", required=True, doc="top module name"),
    "rtl": F("list", required=True, item=F("path"), doc="RTL source files (Verilog/SystemVerilog)"),
    "include_dirs": F("list", item=F("path"), default=[]),
    "defines": F("map", item=F("str"), default={}),
    "profiles": F("obj", required=True, fields={
        "tools": F("str", required=True, doc="tool profile name (in the config dir) or path"),
        "technology": F("str", required=True, doc="technology profile name or path"),
        "corner": F("str", doc="override the technology default corner"),
    }),
    "clocks": F("list", required=True, item=CLOCK, doc="explicit timing goal(s); at least one"),
    "resets": F("list", default=[], item=F("obj", fields={
        "port": F("str", required=True),
        "active": F("enum", choices=("high", "low"), default="high"),
        "synchronous": F("bool", default=True),
    })),
    "sdc": F("path", doc="advanced: user SDC used verbatim instead of the generated constraints"),
    "io_delay_fraction": F("float", min=0, max=0.9, default=0.1, doc="input/output delay as a fraction of the period"),
    "activity": F("obj", required=True, doc="switching-activity generation for power", fields={
        "mode": F("enum", required=True, choices=("testbench", "generated"), doc="user testbench or Cedar-generated random stimulus"),
        "testbench": F("path", doc="activity testbench (instantiates the DUT)"),
        "tb_top": F("str", default="Top"),
        "dut_instance": F("str", default="DUT"),
        "cycles": F("int", min=1, default=1000, doc="cycles for generated stimulus"),
        "seed": F("int", default=1),
    }),
    "verification": F("obj", required=True, fields={
        "mode": F("enum", required=True, choices=("conformal", "simulation", "circt-lec", "external"),
                  doc="candidate correctness backend (fail-closed)"),
        "testbench": F("path", doc="self-checking testbench (simulation mode) that prints pass_marker"),
        "pass_marker": F("str", default="CEDAR_TB_PASS"),
        "fail_marker": F("str", default="CEDAR_TB_FAIL"),
        "assumptions": F("list", item=F("str"), default=[], doc="documented reset/initial-state assumptions"),
        "conformal_extra": F("list", item=F("str"), default=[], doc="extra dofile commands (e.g. add pin constraints)"),
    }),
    "objective": F("obj", fields={
        "preset": F("enum", choices=("balanced", "area", "delay", "power", "paper-power", "area-delay")),
        "weights": F("obj", fields={
            "area": F("float", min=0), "delay": F("float", min=0), "power": F("float", min=0),
        }),
        "normalization": F("enum", choices=("baseline", "absolute"), default="baseline"),
        "require_timing_met": F("bool", default=True),
        "weight_schedule": F("list", default=[], item=F("obj", fields={
            "from_iteration": F("int", required=True, min=1),
            "weights": F("obj", required=True, fields={
                "area": F("float", min=0), "delay": F("float", min=0), "power": F("float", min=0)}),
        })),
    }),
    "measurement": F("obj", fields={
        "mode": F("enum", choices=("full-ppa", "synthesis-only"), default="full-ppa",
                  doc="full-ppa = DC + Innovus + activity sim + PrimeTime (strict)"),
        "approximate_pruning": F("bool", default=False,
                                 doc="allow skipping P&R/power for DC-dominated candidates (heuristic; never in strict mode)"),
    }),
    "search": F("obj", fields={
        "preset": F("enum", choices=("quick", "paper"), default="quick"),
        "max_iterations": F("int", min=1),
        "min_iterations": F("int", min=0),
        "min_cost_configs": F("int", min=0),
        "min_rule_combos": F("int", min=0),
        "stall_window": F("int", min=1),
        "direct_variants": F("int", min=0, doc="direct-rewrite candidates requested per round"),
        "direct_rounds": F("int", min=1, doc="maximum direct-rewrite rounds"),
        "direct_min_rounds": F("int", min=1, doc="rounds before objective-based convergence may stop the direct search"),
        "rules_to_propose": F("int", min=0),
        "eqsat_iterations": F("int", min=1, doc="egglog run iterations (omit = saturate)"),
        "eqsat_timeout_s": F("int", min=1),
        "run_timeout_s": F("int", min=60, unit="s",
                           doc="overall wall-clock budget of the run from its creation, including agent work and "
                               "every resume (quick: 3600, paper: 172800). When it expires, running tool "
                               "processes are terminated and the run ends as failed; it cannot complete."),
        "allow_latency_change": F("bool", default=False, doc="permit pipeline/retiming rewrites (off: no-retiming mode)"),
        "hierarchical": F("bool", default=False, doc="optimize module bodies independently while preserving hierarchy"),
        "opaque_modules": F("list", item=F("str"), default=[],
                            doc="exact module symbols excluded from optimization; requires hierarchical search"),
        "seed_rules": F("bool", default=True, doc="include Cedar's shipped seed rule library in the rule pool"),
    }),
    "agent": F("obj", fields={
        "stage_models": F("map", item=F("str"), default={},
                          doc="optional model per stage (proposal, selection, feedback); recorded in provenance"),
    }),
    "notes": F("str", default=""),
}
