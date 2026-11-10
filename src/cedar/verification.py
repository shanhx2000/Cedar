# SPDX-License-Identifier: Apache-2.0
"""Candidate correctness checking (fail-closed).

Backends (selected explicitly by ``verification.mode`` in the design spec):

* ``conformal`` — Cadence Conformal LEC, original RTL (golden) vs candidate
  RTL (revised); key-point mapping by name and function. Passes only on
  ``Compare Results: PASS`` with no incomplete verification items.
* ``simulation`` — a *self-checking* testbench that prints a pass marker.
  Activity-only testbenches are never accepted as checking evidence.
* ``circt-lec`` — open-source combinational equivalence: the toolchain's
  ``circt-lec`` emits an SMT-LIB miter solved with Z3. Only for designs
  without state (it reports ``unavailable`` for sequential designs).

Every result is one of ``passed``, ``failed``, ``unavailable`` or ``error``;
only ``passed`` admits a candidate. An unavailable backend is a hard stop for
optimization runs (no silent downgrade to "assume pass").
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Sequence

from . import proc
from .eda import flow
from .egraph import pipeline
from .toolchain import Toolchain

RESULTS = ("passed", "failed", "unavailable", "error")


@dataclass
class CheckResult:
    status: str
    backend: str
    detail: str
    seconds: float
    evidence: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict:
        return asdict(self)


def check(mode: str, golden: Sequence[str], candidate: Sequence[str], top: str, work: Path,
          ctx: Optional[flow.FlowContext] = None, toolchain: Optional[Toolchain] = None,
          ver_cfg: Optional[dict] = None) -> CheckResult:
    ver_cfg = ver_cfg or {}
    t0 = time.monotonic()
    work.mkdir(parents=True, exist_ok=True)
    try:
        if mode == "conformal":
            if ctx is None:
                return CheckResult("unavailable", mode, "no EDA tool profile", 0.0)
            r = flow.conformal_lec(ctx, list(golden), list(candidate), work, ver_cfg.get("conformal_extra") or [])
            p = flow.parse_conformal(r)
            return CheckResult(str(p["status"]), mode, str(p["detail"]), round(time.monotonic() - t0, 2),
                               r.outputs.get("verification_rpt", ""))
        if mode == "external":
            if ctx is None:
                return CheckResult("unavailable", mode, "no EDA tool profile", 0.0)
            p = flow.external_check(ctx, list(golden), list(candidate), work)
            return CheckResult(str(p["status"]), mode, str(p["detail"]), round(time.monotonic() - t0, 2),
                               str(work / "verdict.json"))
        if mode == "simulation":
            if ctx is None:
                return CheckResult("unavailable", mode, "no EDA tool profile", 0.0)
            tb = (ver_cfg.get("testbench") or {}).get("path")
            if not tb:
                return CheckResult("unavailable", mode, "no self-checking testbench configured", 0.0)
            p = flow.rtl_sim_check(ctx, list(candidate), tb, ver_cfg.get("pass_marker", "CEDAR_TB_PASS"),
                                   ver_cfg.get("fail_marker", "CEDAR_TB_FAIL"), work)
            return CheckResult(str(p["status"]), mode, str(p["detail"]), round(time.monotonic() - t0, 2),
                               str(work / "sim_check.log"))
        if mode == "circt-lec":
            if toolchain is None or not toolchain.has("circt-lec"):
                return CheckResult("unavailable", mode, "circt-lec not in the selected toolchain", 0.0)
            return _circt_lec(toolchain, golden, candidate, top, work, t0, ctx)
        return CheckResult("error", mode, f"unknown verification mode {mode!r}", 0.0)
    except proc.DeadlineExpired:
        raise                                  # the run budget ran out: no verdict, the run expires
    except Exception as exc:  # noqa: BLE001 - any crash is an error, never a pass
        return CheckResult("error", mode, f"{type(exc).__name__}: {exc}", round(time.monotonic() - t0, 2))


def _circt_lec(tc: Toolchain, golden: Sequence[str], candidate: Sequence[str], top: str, work: Path,
               t0: float, ctx: Optional[flow.FlowContext] = None) -> CheckResult:
    import z3

    g = work / "golden.mlir"
    c = work / "candidate.mlir"
    inc = ctx.include_dirs if ctx else ()
    dfn = ctx.defines if ctx else None
    pipeline.frontend(tc, golden, top, g, work, inc, dfn)
    pipeline.frontend(tc, candidate, top, c, work, inc, dfn)
    for f in (g, c):
        if re.search(r"\bseq\.(firreg|compreg)\b|\bllhd\.", f.read_text()):
            return CheckResult("unavailable", "circt-lec",
                               "design has state; circt-lec mode supports combinational designs only "
                               "(use conformal or simulation)", round(time.monotonic() - t0, 2))
    ctext = c.read_text().replace(f"@{top}(", f"@{top}__cand(")
    c.write_text(ctext)
    smt = work / "lec.smt2"
    pipeline._run(tc, "circt-lec", [tc.tool("circt-lec"), str(g), str(c), f"-c1={top}", f"-c2={top}__cand",
                                    "--emit-smtlib", "-o", str(smt)], work, 300)
    # circt-lec emits "(check-sat)" / "(reset)" commands; strip them so that
    # "(reset)" cannot clear the miter assertion before solving.
    text = re.sub(r"^\((check-sat|reset)\)\s*$", "", smt.read_text(), flags=re.M)
    if "(assert" not in text:
        return CheckResult("error", "circt-lec", "no miter assertion in circt-lec output", round(time.monotonic() - t0, 2))
    s = z3.Solver()
    # capped by the run budget; never 0 (= unlimited): bounded_ms raises instead
    s.set("timeout", proc.bounded_ms(120000))
    s.add(z3.parse_smt2_string(text))
    r = s.check()
    proc.check()                               # a solve cut short by the budget gives no verdict
    secs = round(time.monotonic() - t0, 2)
    if r == z3.unsat:
        return CheckResult("passed", "circt-lec", "SMT miter UNSAT (equivalent)", secs, str(smt))
    if r == z3.sat:
        return CheckResult("failed", "circt-lec", "SMT miter SAT (counterexample exists)", secs, str(smt))
    return CheckResult("error", "circt-lec", f"solver returned {r} ({s.reason_unknown()})", secs, str(smt))
