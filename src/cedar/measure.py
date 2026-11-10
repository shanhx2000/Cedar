# SPDX-License-Identifier: Apache-2.0
"""Measure one design point: DC -> Innovus -> activity simulation -> PrimeTime.

``full-ppa`` mode is strict: every stage must succeed for the point to have a
complete PPA record; a failed or timed-out stage yields explicit
``failed``/``missing`` metrics and the point can never enter the frontier.

Approximate pruning (A10): when explicitly enabled, a candidate whose DC
area, delay *and* DC vectorless power are all no better than the baseline's
skips P&R/power and is recorded as ``skipped`` (unmeasured), never as a
measured point. Vectorless DC dominance does not prove SAIF/post-route
dominance, so pruning is disabled in strict mode and off by default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .eda import flow
from .eda.runner import StageResult
from .metrics import Metric, PPA


@dataclass
class Measurement:
    ppa: PPA
    stages: List[StageResult] = field(default_factory=list)
    pruned: bool = False
    info: Dict = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """All stages succeeded and every metric required by the mode is valid."""
        need = ["area", "delay"] + ([] if self.ppa.measurement_mode == "synthesis-only" else ["power"])
        return all(s.ok for s in self.stages) and not self.pruned and not self.ppa.require(need)

    def to_dict(self) -> dict:
        return {"ppa": self.ppa.to_dict(), "stages": [s.to_dict() for s in self.stages],
                "pruned": self.pruned, "complete": self.complete, "info": self.info}


def _dc_vectorless_power_uw(power_rpt: Path) -> Optional[float]:
    if not power_rpt.is_file():
        return None
    txt = power_rpt.read_text(errors="replace")

    def grab(label: str) -> Optional[float]:
        m = re.search(label + r"\s*=\s*([0-9.eE+-]+)\s*(mW|uW|nW|pW|W)", txt)
        if not m:
            return None
        return float(m.group(1)) * {"W": 1e6, "mW": 1e3, "uW": 1.0, "nW": 1e-3, "pW": 1e-6}[m.group(2)]
    dyn, leak = grab("Total Dynamic Power"), grab("Cell Leakage Power")
    return None if dyn is None else dyn + (leak or 0.0)


def measure(ctx: flow.FlowContext, rtl: List[str], work: Path, activity: dict, mode: str = "full-ppa",
            constraints_id: str = "", prune_against: Optional[dict] = None) -> Measurement:
    """Run the configured measurement stages for ``rtl`` under ``work``."""
    ppa = PPA(constraints_id=constraints_id, measurement_mode=mode)
    m = Measurement(ppa)
    syn = flow.synthesize(ctx, rtl, work / "synth")
    m.stages.append(syn)
    ppa.metrics.update(flow.synth_metrics(ctx, syn))
    slack = ppa.metrics.get("slack")
    ppa.timing_met = (slack.value >= 0) if slack is not None and slack.ok else None
    dcp = _dc_vectorless_power_uw(Path(syn.outputs.get("power_rpt", ""))) if syn.ok else None
    m.info["dc_vectorless_power_uW"] = dcp
    if flow.is_external(ctx) and mode != "synthesis-only":
        # external-ppa measures every metric in one post-synthesis stage
        ppa.metrics["power"] = flow.external_power_metric(syn)
        return m
    if mode == "synthesis-only":
        ppa.metrics["power"] = Metric.unavailable("power", "post-route", "primetime", "skipped",
                                                  "synthesis-only measurement mode")
        return m
    if not syn.ok:
        ppa.metrics["power"] = Metric.unavailable("power", "post-route", "primetime", "failed", "synthesis failed")
        return m
    if prune_against is not None:
        dc = {"area": ppa.get("area"), "delay": ppa.get("delay"), "power": dcp}
        if all(dc[k] is not None and prune_against.get(k) and dc[k] >= prune_against[k] for k in dc):
            m.pruned = True
            ppa.metrics["power"] = Metric.unavailable(
                "power", "post-route", "primetime", "skipped",
                "approximate DC-dominance pruning: P&R/power not run (heuristic)")
            return m
    pnr = flow.place_route(ctx, syn.outputs["netlist"], syn.outputs["sdc"], work / "pnr")
    m.stages.append(pnr)
    if not pnr.ok:
        ppa.metrics["power"] = Metric.unavailable("power", "post-route", "primetime", "failed", "P&R failed")
        return m
    t = flow.parse_timing(Path(pnr.outputs["timing_rpt"]))
    m.info["post_route_slack_libunit"] = t["slack"]
    act = flow.activity_sim(ctx, pnr.outputs["netlist"], pnr.outputs["sdf"], activity["testbench"],
                            activity["tb_top"], activity["dut_instance"], work / "activity")
    m.stages.append(act)
    if not act.ok:
        ppa.metrics["power"] = Metric.unavailable("power", "post-route", "primetime", "failed", "activity simulation failed")
        return m
    pw = flow.power_pt(ctx, pnr.outputs["netlist"], pnr.outputs["spef"], act.outputs["saif"],
                       syn.outputs["sdc"], activity["tb_top"], activity["dut_instance"], work / "power")
    m.stages.append(pw)
    ppa.metrics["power"] = flow.power_metric(pw)
    cov = flow.parse_saif_coverage(Path(pw.log)) if pw.ok else None
    m.info["saif_annotated_nets_pct"] = cov
    if pw.ok and (cov is None or cov < 90.0):
        # Power computed mostly from default/propagated activity is not an
        # activity-based measurement; refuse to report it as measured.
        ppa.metrics["power"] = Metric.unavailable(
            "power", "post-route", "primetime:saif+spef", "failed",
            f"SAIF annotated only {cov}% of nets (< 90%); check tb_top/dut_instance")
    return m
