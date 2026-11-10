# SPDX-License-Identifier: Apache-2.0
"""The Cedar optimization workflow as a resumable state machine.

Phases (persisted in ``<run>/run.json``; every phase is idempotent):

  preflight -> baseline -> analysis -> direct_task -> direct_eval -> rules_task
  -> rules_admit -> probe -> [select -> eqsat_extract -> check -> measure -> decide]*
  -> attribution -> report -> done

Agent work happens only in *tasks* (see :mod:`cedar.workflow.tasks`). When a
task is pending the engine stops with status ``awaiting_agent`` and prints the
task instructions; ``cedar run continue`` resumes after ``cedar task submit``.

Correctness gates: rules enter e-graphs only as Z3-certified exact-width
instances; every candidate (direct, extracted or agent-reconstructed) must pass
the configured equivalence backend before measurement; candidates with failed
checks are excluded from the frontier and the final results.
"""

from __future__ import annotations

import concurrent.futures as cf
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import time
import traceback
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import __version__
from .. import config as cfgmod
from .. import proc as procmod
from .. import verification
from ..egraph import costmodel, pipeline
from ..eda import flow
from ..eda.runner import ToolEnv
from ..frontier import Frontier, Point
from ..measure import measure
from ..metrics import PPA
from ..objective import Objective
from ..rules import certify
from ..selection import History, Probe, complementary_group, design_features, lhs_op_kinds, normalize_rule_name, rank
from ..toolchain import Toolchain, resolve as resolve_toolchain
from . import presets
from .tasks import Task, TaskOutput, now, render, run_agent_command, validate_outputs

PHASES = ["preflight", "baseline", "analysis", "direct_task", "direct_eval", "rules_task", "rules_admit",
          "probe", "iterate", "measure_pending", "attribution", "report", "done"]

AWAIT = "awaiting_agent"


class WorkflowError(RuntimeError):
    pass


def _sha_file(p: str) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _rel(p, base: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(base.resolve()))
    except ValueError:
        return str(p)


class Run:
    def __init__(self, run_dir: Path):
        self.dir = Path(run_dir).resolve()
        self.state_path = self.dir / "run.json"
        if not self.state_path.is_file():
            raise WorkflowError(f"no Cedar run at {self.dir}")
        self.s = json.loads(self.state_path.read_text())
        self._load_configs()

    # ------------------------------------------------------------ creation

    @classmethod
    def create(cls, design_path: str, work_dir: str, tools_ref: Optional[str] = None,
               tech_ref: Optional[str] = None, toolchain_prefix: Optional[str] = None,
               preset: Optional[str] = None, run_id: Optional[str] = None,
               agent: Optional[Dict[str, str]] = None, run_timeout_s: Optional[int] = None) -> "Run":
        design = cfgmod.load_design(design_path)
        dd = design.data
        tools = cfgmod.load_tool_profile(tools_ref or dd["profiles"]["tools"], design.path.parent)
        tech = cfgmod.load_technology(tech_ref or dd["profiles"]["technology"], design.path.parent)
        corner = dd["profiles"].get("corner") or tech.data["default_corner"]
        if corner not in tech.data["corners"]:
            raise WorkflowError(f"corner {corner!r} not in technology {tech.data['name']}")
        search = dict(dd.get("search") or {})
        if preset:
            search["preset"] = preset
        if run_timeout_s is not None:
            search["run_timeout_s"] = int(run_timeout_s)
        settings = presets.resolve(search)
        objective = Objective.from_config(dd.get("objective"))
        rid = run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + dd["name"]
        rdir = Path(work_dir).expanduser().resolve() / rid
        if (rdir / "run.json").exists():
            raise WorkflowError(f"run {rdir} already exists; use `cedar run continue`")
        if any(ch.isspace() for ch in str(rdir)):
            raise WorkflowError(f"work directory {rdir} contains whitespace; EDA scripts need a path without spaces")
        rdir.mkdir(parents=True, exist_ok=False)
        src = rdir / "inputs"
        (src / "rtl").mkdir(parents=True)
        # Tools read the original files (so relative `include`s keep working);
        # numbered copies are kept for provenance and every resume re-checks the
        # originals' hashes.
        rtl = [str(Path(f["path"]).resolve()) for f in dd["rtl"]]
        snapshot = []
        for i, f in enumerate(rtl):
            dst = src / "rtl" / f"{i:02d}_{Path(f).name}"
            shutil.copy(f, dst)
            snapshot.append(str(dst))
        tc = resolve_toolchain(toolchain_prefix, (tools.data.get("toolchain") or {}).get("prefix", {}).get("path")
                               if (tools.data.get("toolchain") or {}).get("prefix") else None)
        state = {
            "schema": "cedar/run@1",
            "cedar_version": __version__,
            "run_id": rid,
            "created_at": now(),
            "status": "running",
            "phase": "preflight",
            "design_file": str(design.path),
            "tools_file": str(tools.path),
            "technology_file": str(tech.path),
            "toolchain_prefix": toolchain_prefix or os.environ.get("CEDAR_TOOLCHAIN_PREFIX") or "",
            "corner": corner,
            "rtl": rtl,
            "rtl_snapshot": snapshot,
            "rtl_sha256": {r: _sha_file(r) for r in rtl},
            "top": dd["top"],
            "constraints_id": cfgmod.constraints_id(design, tech, corner),
            "objective": objective.to_dict(),
            "settings": settings,
            # Overall execution budget (wall clock from creation, across resumes).
            "budget": {"seconds": int(settings["run_timeout_s"]), "started_at_epoch": time.time(),
                       "deadline_epoch": time.time() + int(settings["run_timeout_s"])},
            "measurement": dd.get("measurement") or {"mode": "full-ppa", "approximate_pruning": False},
            "provenance": {
                "design": design.provenance(), "tools": tools.provenance(), "technology": tech.provenance(),
                "toolchain": tc.identity(), "python": sys.version.split()[0],
                "agent": agent or {}, "stage_models": (dd.get("agent") or {}).get("stage_models", {}),
            },
            "timings": {},
            "tasks": {},
            "candidates": {},
            "rules": {},
            "iterations": [],
            "iteration": 0,
            "log": [],
        }
        (rdir / "run.json").write_text(json.dumps(state, indent=2))
        snap = rdir / "config-snapshot"
        snap.mkdir()
        for name, ld in (("design", design), ("tools", tools), ("technology", tech)):
            (snap / f"{name}.json").write_text(json.dumps(ld.provenance(), indent=2, default=str))
        run = cls(rdir)
        # Fingerprint every input that can change behaviour or constraints, so a
        # resumed run can prove it still measures under the same conditions.
        run.s["input_fingerprints"] = run._input_fingerprints()
        run.save()
        return run

    # ------------------------------------------------------------ helpers

    def _load_configs(self) -> None:
        self.design = cfgmod.load_design(self.s["design_file"])
        self.tools = cfgmod.load_tool_profile(self.s["tools_file"])
        self.tech = cfgmod.load_technology(self.s["technology_file"])
        self.objective = Objective.from_config(self.s["objective"])
        self.settings = self.s["settings"]
        self.tenv = ToolEnv(self.tools)
        dd = self.design.data
        self.ctx = flow.FlowContext(
            tools=self.tenv, tech=self.tech, corner=self.s["corner"], top=dd["top"], clocks=dd["clocks"],
            io_delay_fraction=dd["io_delay_fraction"],
            sdc_path=dd["sdc"]["path"] if dd.get("sdc") else None,
            defines=dict(dd.get("defines") or {}),
            include_dirs=[i["path"] for i in dd.get("include_dirs") or []],
        )
        self._tc: Optional[Toolchain] = None

    @property
    def tc(self) -> Toolchain:
        if self._tc is None:
            prof = (self.tools.data.get("toolchain") or {}).get("prefix")
            self._tc = resolve_toolchain(self.s.get("toolchain_prefix") or None, prof["path"] if prof else None)
        return self._tc

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=2, default=str))
        os.replace(tmp, self.state_path)

    def log(self, msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        self.s["log"].append(line)

    def time_phase(self, name: str, seconds: float) -> None:
        self.s["timings"][name] = round(self.s["timings"].get(name, 0.0) + seconds, 2)

    @property
    def verification_mode(self) -> str:
        return self.design.data["verification"]["mode"]

    def baseline_ppa(self) -> Optional[PPA]:
        b = self.s["candidates"].get("baseline")
        return PPA.from_dict(b["measurement"]["ppa"]) if b and b.get("measurement") else None

    def clock_desc(self) -> str:
        c = self.design.data["clocks"][0]
        return f"clock `{c.get('port') or '(virtual)'}` period {c['period_ns_resolved']} ns"

    # ------------------------------------------------------------ driver

    def advance(self, agent_cmd: Optional[str] = None, agent_label: Optional[Dict[str, str]] = None,
                stop_after: Optional[str] = None) -> str:
        """Run phases until done, failure, or an agent task is pending.

        Holds an exclusive lock on the run directory so two processes can never
        advance the same run concurrently. The persisted run budget is installed
        before anything else: an expired run fails (sticky) without starting a
        single process, and the resume checks (input fingerprints, tool identity
        probes) run under the remaining budget.
        """
        import fcntl
        lock = open(self.dir / ".lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise WorkflowError(f"run {self.dir} is already being advanced by another process; "
                                "poll `cedar run status` instead")
        try:
            # Every subprocess started while advancing is capped by the budget.
            procmod.set_deadline(self._deadline_epoch())
            if self.s["phase"] == "done":
                return self._advance(agent_cmd, agent_label, stop_after)    # finished: nothing to check
            if self.s.get("expired"):
                return self._expire(self.s["expired"]["during"])
            if self.budget_expired():
                return self._expire(self.s["phase"])
            try:
                self._check_inputs_unchanged()
            except (WorkflowError, procmod.DeadlineExpired):
                if self.budget_expired():          # probes cut short by the budget: not an input change
                    return self._expire("resume checks")
                raise
            if self.budget_expired():
                return self._expire("resume checks")
            return self._advance(agent_cmd, agent_label, stop_after)
        finally:
            procmod.set_deadline(None)
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    # ------------------------------------------------------------ budget

    def _deadline_epoch(self) -> Optional[float]:
        return (self.s.get("budget") or {}).get("deadline_epoch")

    def budget_expired(self) -> bool:
        d = self._deadline_epoch()
        return d is not None and time.time() >= d

    def _expire(self, where: str) -> str:
        """Record that the overall budget ran out; the run can never complete."""
        b = self.s.get("budget") or {}
        elapsed = round(time.time() - b.get("started_at_epoch", time.time()), 1)
        if not self.s.get("expired"):
            self.s["expired"] = {"during": where, "at": now(), "budget_s": b.get("seconds"), "elapsed_s": elapsed}
            self.log(f"run budget of {b.get('seconds')} s expired during {where} (elapsed {elapsed} s)")
        self.s["status"] = "failed"
        self.s["final_status"] = "failed"
        self.s["error"] = (f"run budget of {b.get('seconds')} s expired during {self.s['expired']['during']}; "
                           "running tools were stopped and the run cannot complete "
                           "(start a new run with a larger search.run_timeout_s / --run-timeout-s)")
        rp = self.dir / "report.json"
        if rp.is_file():                       # a report written as the budget ran out is withdrawn
            rep = json.loads(rp.read_text())
            rep["budget"] = {"seconds": b.get("seconds"), "expired": True, **self.s["expired"]}
            c = rep.get("execution_contract") or {}
            c["status"] = "incomplete"
            c["problems"] = list(c.get("problems") or []) + ["run budget expired"]
            rep["execution_contract"] = c
            rep["final_status"] = "failed"
            rp.write_text(json.dumps(rep, indent=2, default=str))
        self.save()
        return "failed"

    def _check_inputs_unchanged(self) -> None:
        """Refuse to resume when the configuration or original RTL changed."""
        changed = []
        prov = self.s["provenance"]
        for name, ld in (("design", self.design), ("tools", self.tools), ("technology", self.tech)):
            if prov.get(name, {}).get("sha256") not in (None, ld.sha256):
                changed.append(f"{name} spec {ld.path}")
        for path, sha in (self.s.get("rtl_sha256") or {}).items():
            p = Path(path) if os.path.isabs(path) else None
            if p is not None and (not p.is_file() or _sha_file(str(p)) != sha):
                changed.append(f"RTL {path}")
        stored = self.s.get("input_fingerprints")
        if stored is not None:
            now_fp = self._input_fingerprints()
            for path in sorted(set(stored) | set(now_fp)):
                if stored.get(path) != now_fp.get(path):
                    what = "added" if path not in stored else ("removed" if path not in now_fp else "changed")
                    changed.append(f"{stored.get(path, now_fp.get(path, ['?']))[0]} {path} {what}")
        ident = self.s["provenance"].get("tool_identity")
        if ident:
            # Same tools as at preflight: path, real path, content, environment
            # AND the probed version (a wrapper may dispatch to another release).
            from ..setup import identity_changes, tool_identity
            changed += [f"tool {c}" for c in identity_changes(ident, tool_identity(self.tools))]
        cid = cfgmod.constraints_id(self.design, self.tech, self.s["corner"])
        if cid != self.s["constraints_id"]:
            changed.append(f"constraints id {self.s['constraints_id']} -> {cid}")
        if changed:
            raise WorkflowError("inputs changed since this run started (" + "; ".join(changed) +
                                "); measurements would no longer be comparable — start a new run")

    _INCLUDE_RE = re.compile(r'^\s*`include\s+"([^"]+)"', re.M)

    def _hdl_closure(self, files: List[str]) -> List[str]:
        """Files plus every `include they reach (resolved like the frontends)."""
        incs = [Path(i["path"]) for i in self.design.data.get("include_dirs") or []]
        seen: List[str] = []
        todo = [str(Path(f).resolve()) for f in files]
        while todo:
            f = todo.pop()
            if f in seen:
                continue
            seen.append(f)
            try:
                text = Path(f).read_text(errors="replace")
            except OSError:
                continue
            for inc in self._INCLUDE_RE.findall(text):
                for d in [Path(f).parent] + incs:
                    cand = (d / inc).resolve()
                    if cand.is_file():
                        todo.append(str(cand))
                        break
                else:
                    seen.append(f"<unresolved include {inc} from {f}>")
        return seen

    def _input_fingerprints(self) -> Dict[str, list]:
        """Content hashes of behaviour/constraint inputs; size+mtime for libraries."""
        dd = self.design.data
        fp: Dict[str, list] = {}

        def content(kind: str, path: Optional[str]) -> None:
            if not path:
                return
            if path.startswith("<unresolved"):
                fp[path] = [kind, "unresolved"]
                return
            p = Path(path)
            fp[str(p)] = [kind, _sha_file(str(p)) if p.is_file() else "missing"]

        for f in self._hdl_closure(self.s["rtl"]):
            content("hdl", f)
        if dd.get("sdc"):
            content("sdc", dd["sdc"]["path"])
        for kind, cfg in (("activity-testbench", dd.get("activity") or {}),
                          ("checking-testbench", dd.get("verification") or {})):
            if cfg.get("testbench"):
                for f in self._hdl_closure([cfg["testbench"]["path"]]):
                    content(kind, f)
        for sec, key in (("synthesis", "pre_compile_hook"), ("synthesis", "post_compile_hook"),
                         ("place_route", "post_init_hook"), ("place_route", "pre_route_hook")):
            content("tcl-hook", self.ctx.hook(sec, key))
        content("setup-script", self.tenv.setup_script)
        try:
            libs = self.ctx.dbs() + self.ctx.libs() + self.ctx.sim_models() + [self.ctx.rc_tech()] + self.ctx.lefs()
        except (KeyError, TypeError):
            libs = []
        for f in libs:
            p = Path(f)
            st = p.stat() if p.exists() else None
            fp[str(p)] = ["library", f"{st.st_size}:{int(st.st_mtime)}" if st else "missing"]
        return fp

    def _advance(self, agent_cmd: Optional[str], agent_label: Optional[Dict[str, str]],
                 stop_after: Optional[str]) -> str:
        while True:
            phase = self.s["phase"]
            if self.s.get("expired"):
                return self._expire(self.s["expired"]["during"])
            if phase == "done":
                self.s["status"] = self.s.get("final_status", "completed")
                self.save()
                return self.s["status"]
            if self.budget_expired():
                return self._expire(phase)
            fn = getattr(self, f"phase_{phase}")
            t0 = time.monotonic()
            try:
                res = fn()
            except Exception as exc:  # noqa: BLE001
                if self.budget_expired():
                    self.time_phase(phase, time.monotonic() - t0)
                    return self._expire(phase)
                self.s["status"] = "failed"
                self.s["error"] = f"{phase}: {type(exc).__name__}: {exc}"
                self.s["traceback"] = traceback.format_exc()[-4000:]
                self.time_phase(phase, time.monotonic() - t0)
                self.log(f"FAILED in {phase}: {exc}")
                self.save()
                return "failed"
            self.time_phase(phase, time.monotonic() - t0)
            if self.budget_expired():
                # Whatever the phase produced under an expired budget (timed-out
                # stages, partial iterations) is never credited.
                return self._expire(phase)
            if res == AWAIT:
                task = self.pending_task()
                if agent_cmd and task is not None:
                    self.log(f"running unattended agent for task {task.id} ({task.kind})")
                    for o in task.outputs:          # stale outputs of an earlier attempt never count
                        Path(o.path).unlink(missing_ok=True)
                    info = run_agent_command(agent_cmd, task, self.dir)
                    self.time_phase(f"agent:{task.kind}", float(info["seconds"]))
                    if self.budget_expired():
                        return self._expire(f"agent:{task.kind}")
                    if info["timed_out"] or info["returncode"] != 0:
                        why = ("timed out" if info["timed_out"] else f"exited with status {info['returncode']}")
                        rec = self.s["tasks"][task.id]
                        rec["attempts"] = int(rec.get("attempts") or 0) + 1
                        rec["status"] = "pending"
                        rec["feedback"] = f"unattended agent {why}; its outputs were not accepted (log: {info['log']})"
                        self.log(f"task {task.id}: {rec['feedback']}")
                        self.save()
                        if rec["attempts"] >= 2:
                            self.s["status"] = "failed"
                            self.s["error"] = f"agent task {task.id}: unattended agent {why} twice"
                            self.save()
                            return "failed"
                        continue
                    ok, msg = self.submit(task.id, agent_label or {"adapter": agent_cmd.split()[0]})
                    self.log(f"task {task.id}: {'accepted' if ok else 'rejected: ' + msg}")
                    if not ok and self.s["tasks"][task.id]["attempts"] >= 2:
                        self.s["status"] = "failed"
                        self.s["error"] = f"agent task {task.id} rejected twice: {msg}"
                        self.save()
                        return "failed"
                    continue
                self.s["status"] = AWAIT
                self.save()
                return AWAIT
            if res in ("failed", "incomplete", "infeasible_timing_goal", "missing_capability"):
                self.s["status"] = res
                self.save()
                return res
            self.s["phase"] = PHASES[PHASES.index(phase) + 1] if res == "next" else res
            self.s["status"] = "running"
            self.save()
            if stop_after and phase == stop_after:
                return "running"

    # ------------------------------------------------------------ tasks

    def pending_task(self) -> Optional[Task]:
        for tid, t in self.s["tasks"].items():
            if t["status"] != "accepted":
                return Task.from_dict(t)
        return None

    def new_task(self, kind: str, prompt: str, outputs: List[TaskOutput], meta: Optional[dict] = None) -> Task:
        tid = f"t{len(self.s['tasks']) + 1:03d}-{kind}"
        tdir = self.dir / "tasks"
        tdir.mkdir(exist_ok=True)
        pf = tdir / f"{tid}.md"
        pf.write_text(prompt)
        stage = {"direct_rewrite": "proposal", "propose_rules": "proposal", "select_rules": "selection",
                 "extraction_fallback": "proposal", "stop_decision": "feedback"}[kind]
        t = Task(tid, kind, str(pf), outputs, created_at=now(), meta=meta or {},
                 stage_model=(self.s["provenance"].get("stage_models") or {}).get(stage, ""))
        self.s["tasks"][tid] = t.to_dict()
        self.save()
        return t

    def task_of(self, kind: str, key: str = "") -> Optional[Task]:
        for t in self.s["tasks"].values():
            if t["kind"] == kind and t.get("meta", {}).get("key", "") == key:
                return Task.from_dict(t)
        return None

    def _check_verilog(self, p: Path) -> Optional[str]:
        work = self.dir / "tmp" / ("parse-" + p.stem)
        work.mkdir(parents=True, exist_ok=True)
        try:
            pipeline.frontend(self.tc, [str(p)], self.s["top"], work / "x.mlir", work,
                              self.ctx.include_dirs, self.ctx.defines, flatten=False)
        except pipeline.PipelineError as exc:
            return f"does not parse/elaborate as top `{self.s['top']}`: {str(exc)[:300]}"
        sig = _module_signature((work / "x.mlir").read_text(), self.s["top"])
        base = self.s.get("baseline_signature")
        if base is not None and sig != base:
            return f"port list differs from the original ({sig} vs {base})"
        return None

    def _check_egg(self, p: Path) -> Optional[str]:
        from ..rules import z3verify as zv
        try:
            forms = zv.parse_top_forms(zv._strip_line_comments(p.read_text()))
        except ValueError as exc:
            return f"malformed s-expression: {exc}"
        n = sum(1 for f in forms if isinstance(f, list) and f and f[0] in ("rule", "rewrite"))
        return None if n == 1 else f"expected exactly one rule form, found {n}"

    def submit(self, task_id: str, agent: Optional[Dict[str, str]] = None) -> Tuple[bool, str]:
        """Validate and accept a task's outputs, within the run budget.

        Output validation starts processes (the frontend parses submitted
        Verilog), so the persisted run deadline is installed for its duration --
        also for a standalone ``cedar task submit`` -- and the tighter of it and
        an enclosing deadline (unattended submission inside ``advance``) applies.
        The enclosing deadline is restored afterwards, never cleared.
        """
        if task_id not in self.s["tasks"]:
            raise WorkflowError(f"unknown task {task_id}")
        outer = procmod.deadline()
        mine = self._deadline_epoch()
        procmod.set_deadline(min((d for d in (outer, mine) if d is not None), default=None))
        try:
            return self._submit(task_id, agent)
        finally:
            procmod.set_deadline(outer)

    def _submit(self, task_id: str, agent: Optional[Dict[str, str]]) -> Tuple[bool, str]:
        expired_msg = "run budget expired; the run cannot accept further work"
        if self.s.get("expired") or self.budget_expired() or procmod.expired():
            self._expire(self.s.get("expired", {}).get("during") or f"task {task_id}")
            return False, expired_msg
        t = Task.from_dict(self.s["tasks"][task_id])
        t.attempts += 1
        t.submitted_at = now()
        if agent:
            t.agent = {k: v for k, v in agent.items() if v}
        try:
            problems = validate_outputs(t, self._check_verilog, self._check_egg)
        except procmod.DeadlineExpired:
            problems = ["run budget expired during validation"]
        if self.budget_expired() or procmod.expired():
            # validation ran into the budget: nothing is accepted, the run fails (sticky)
            self._expire(f"task {task_id} validation")
            return False, expired_msg
        if problems:
            t.status, t.feedback = "pending", "; ".join(problems)
            self.s["tasks"][task_id] = t.to_dict()
            self.save()
            return False, t.feedback
        t.status, t.feedback = "accepted", ""
        self.s["tasks"][task_id] = t.to_dict()
        self.save()
        return True, "accepted"

    # ------------------------------------------------------------ phases

    def required_stages(self) -> List[str]:
        mode = self.s["measurement"].get("mode", "full-ppa")
        stages = ["synthesis"] if mode == "synthesis-only" or flow.is_external(self.ctx) else \
            ["synthesis", "place_route", "simulation", "saif", "power"]
        vm = self.verification_mode
        if vm in ("conformal", "external"):
            stages.append("equivalence")
        elif vm == "simulation" and "simulation" not in stages:
            stages.append("simulation")
        return stages

    def phase_preflight(self) -> str:
        caps: Dict[str, dict] = {}
        missing = []
        for st in self.required_stages():
            r = self.tenv.which(st)
            caps[st] = {"status": r.status, "detail": r.detail}
            if not r.ok:
                missing.append(f"{st}: {r.status} ({r.detail[:120]})")
        tcp = self.tc.probe()
        caps["toolchain"] = {"status": "ok", "detail": tcp}
        if self.verification_mode == "circt-lec" and not self.tc.has("circt-lec"):
            missing.append("circt-lec: not in toolchain")
        if self.objective.weights.get("power", 0) > 0 and self.s["measurement"].get("mode") == "synthesis-only":
            missing.append("objective weights power but measurement.mode is synthesis-only (no valid power)")
        if self.s["measurement"].get("mode", "full-ppa") == "full-ppa" and self.s["measurement"].get("approximate_pruning"):
            self.log("NOTE: approximate DC-dominance pruning enabled (heuristic; pruned candidates stay unmeasured)")
        self.s["capabilities"] = caps
        # Which tools (path, content, version) and environment measure this run.
        from ..setup import tool_identity
        self.s["provenance"]["tool_identity"] = tool_identity(self.tools)
        mlir = self.dir / "inputs" / "original.mlir"
        dd = self.design.data
        pipeline.frontend(self.tc, self.s["rtl"], self.s["top"], mlir, self.dir / "inputs",
                          [i["path"] for i in dd.get("include_dirs") or []], dd.get("defines") or {}, flatten=False)
        self.s["baseline_signature"] = _module_signature(mlir.read_text(), self.s["top"])
        ports = set(re.findall(r"\b(?:in|inout)\s+%?\"?([\w$]+)\"?\s*:", self.s["baseline_signature"]))
        for c in dd["clocks"]:
            if c.get("port") and c["port"] not in ports and not dd.get("sdc"):
                # An unbound create_clock would leave synthesis unconstrained.
                self.s["error"] = (f"clock port {c['port']!r} is not an input of top module {self.s['top']!r} "
                                   f"(inputs: {sorted(ports)}); fix clocks[].port or supply an SDC")
                self.log(self.s["error"])
                return "failed"
        if missing:
            self.s["error"] = "missing required capabilities: " + "; ".join(missing)
            self.log(self.s["error"])
            return "missing_capability"
        self.log(f"preflight ok: stages {', '.join(self.required_stages())}; verification={self.verification_mode}")
        return "next"

    def _activity(self) -> dict:
        a = self.design.data["activity"]
        if a["mode"] == "testbench":
            return {"testbench": a["testbench"]["path"], "tb_top": a["tb_top"], "dut_instance": a["dut_instance"]}
        from ..eda.gen_testbench import generate_activity_testbench
        tb = self.dir / "inputs" / "tb_activity_generated.v"
        if not tb.is_file():
            generate_activity_testbench(self.tc, self.s["rtl"], self.s["top"], tb, self.design.data["clocks"],
                                        self.design.data.get("resets") or [], cycles=a["cycles"], seed=a["seed"],
                                        include_dirs=self.ctx.include_dirs, defines=self.ctx.defines)
        return {"testbench": str(tb), "tb_top": "Top", "dut_instance": "DUT"}

    def _measure(self, name: str, rtl: List[str]) -> dict:
        prune = None
        m = self.s["measurement"]
        if m.get("approximate_pruning") and name != "baseline" and m.get("mode") != "full-ppa-strict":
            b = self.s["candidates"]["baseline"]["measurement"]
            bp = PPA.from_dict(b["ppa"])
            prune = {"area": bp.get("area"), "delay": bp.get("delay"), "power": b["info"].get("dc_vectorless_power_uW")}
        activity = {} if flow.is_external(self.ctx) else self._activity()
        meas = measure(self.ctx, rtl, self.dir / "measure" / name, activity, m.get("mode", "full-ppa"),
                       self.s["constraints_id"], prune)
        return meas.to_dict()

    def phase_baseline(self) -> str:
        self.log("measuring baseline (original RTL)")
        md = self._measure("baseline", self.s["rtl"])
        self.s["candidates"]["baseline"] = {"origin": "baseline", "rtl": self.s["rtl"], "verified": True,
                                            "verification": {"status": "passed", "detail": "reference design"},
                                            "measurement": md}
        ppa = PPA.from_dict(md["ppa"])
        self.log(f"baseline: {ppa.summary()}")
        need = ["area", "delay"] + (["power"] if self.s["measurement"].get("mode", "full-ppa") != "synthesis-only" else [])
        probs = ppa.require(need)
        probs += [f"stage {st.get('stage')} {st.get('status')}: {str(st.get('detail', ''))[:200]}"
                  for st in md.get("stages", []) if st.get("status") != "ok"]
        if probs or not md.get("complete"):
            self.s["error"] = "baseline measurement incomplete: " + ("; ".join(probs) or "measurement not complete")
            return "failed"
        if self.objective.require_timing_met and ppa.timing_met is not True:
            self.s["error"] = (f"infeasible timing goal: baseline misses the declared clock "
                               f"({self.clock_desc()}); slack={ppa.metrics['slack'].value} ns. "
                               "Cedar does not relax the goal; choose a feasible period or set "
                               "objective.require_timing_met: false.")
            self.log(self.s["error"])
            return "infeasible_timing_goal"
        return "next"

    def _eqsat(self, name: str, rules: List[str], iterations: Optional[int]) -> pipeline.EqsatResult:
        dd = self.design.data
        return pipeline.eqsat(self.tc, self.dir / "egraph" / name, self.s["top"], self.s["rtl"], self.s["top"],
                              rules, iterations, self.settings["eqsat_timeout_s"],
                              [i["path"] for i in dd.get("include_dirs") or []], dd.get("defines") or {},
                              hierarchical=self.settings.get("hierarchical", False),
                              opaque_modules=self.settings.get("opaque_modules", []))

    def phase_analysis(self) -> str:
        eq = self._eqsat("analysis", [], 1)
        data = json.loads(Path(eq.egraph_json).read_text())
        self.s["analysis"] = {"egraph": eq.egraph_json, "ops_egg": eq.ops_egg, "n_nodes": eq.n_nodes,
                              "n_classes": eq.n_classes, "widths": eq.widths, "op_histogram": eq.op_histogram,
                              "scales": costmodel.design_scales(data)}
        other = pipeline.non_signless_types_in(Path(eq.ops_egg).read_text() if Path(eq.ops_egg).is_file() else "")
        self.s["analysis"]["non_signless_types"] = other
        self.log(f"analysis e-graph: {eq.n_nodes} nodes, widths {eq.widths}")
        if other:
            self.log(f"design has non-signless types {other}: certified rules (signless integers only) "
                     "never apply to those e-nodes")
        return "next"

    def _direct_round(self) -> int:
        return int(self.s.get("direct_round", 1))

    def _round_context(self, r: int) -> str:
        if r == 1:
            return "This is the first round."
        lines = []
        b = self.baseline_ppa()
        best = None
        for name, c in self.s["candidates"].items():
            if c.get("origin") != "direct":
                continue
            v = c.get("verification", {}).get("status")
            ppa = PPA.from_dict(c["measurement"]["ppa"]) if c.get("measurement") else None
            score = self.objective.score(ppa, b)[0] if ppa else None
            lines.append(f"- {name} (round {c.get('round')}): verification {v}; "
                         f"{ppa.summary() if ppa else 'not measured'}; objective score "
                         f"{'n/a' if score is None else f'{score:.4f}'} (baseline 1.0); change: "
                         f"{(c.get('note') or {}).get('change', '')}; file {c['rtl'][0]}")
            if score is not None and not self._candidate_problems(c) and (best is None or score < best[1]):
                best = (name, score)
        if best:
            lines.append(f"Best so far: {best[0]} (score {best[1]:.4f}). Either push the idea behind it further, "
                         "or try a structurally different approach that targets the objective; avoid repeating "
                         "variants that failed verification.")
        return "\n".join(lines) or "(no candidates from earlier rounds)"

    def phase_direct_task(self) -> str:
        n = int(self.settings["direct_variants"])
        if n <= 0:
            return "next"
        r = self._direct_round()
        key = f"round{r}"
        t = self.task_of("direct_rewrite", key=key)
        if t is None:
            ddir = self.dir / "candidates" / "direct"
            ddir.mkdir(parents=True, exist_ok=True)
            stem = "direct" if r == 1 else f"direct_r{r}"
            outs = [TaskOutput(str(ddir / f"{stem}_{i + 1}.v"), "verilog", required=(i == 0)) for i in range(n)]
            notes = ddir / ("notes.json" if r == 1 else f"notes_r{r}.json")
            outs.append(TaskOutput(str(notes), "json", required=True))
            bp = self.baseline_ppa()
            prompt = render(
                "direct_rewrite", task_id=f"t{len(self.s['tasks']) + 1:03d}-direct_rewrite", top=self.s["top"],
                round=r, round_context=self._round_context(r),
                n_variants=n, rtl_files=", ".join(self.s["rtl"]), clock_desc=self.clock_desc(),
                objective=self.objective.describe(), measurement_mode=self.s["measurement"].get("mode", "full-ppa"),
                baseline_ppa=bp.summary() if bp else "n/a", op_histogram=json.dumps(design_features(self.s["analysis"]["op_histogram"])),
                latency_rule=("Pipeline/retiming changes are NOT allowed (no-retiming mode)."
                              if not self.settings.get("allow_latency_change") else
                              "Latency changes are allowed only if the verification backend supports them."),
                verification_mode=self.verification_mode,
                output_list="\n".join(f"- `{o.path}`" + ("" if o.required else " (optional)") for o in outs[:-1]),
                notes_path=str(notes), run_dir=str(self.dir))
            t = self.new_task("direct_rewrite", prompt, outs, {"key": key, "round": r})
            self._announce(t)
            return AWAIT
        return "next" if t.status == "accepted" else AWAIT

    def _announce(self, t: Task) -> None:
        self.log(f"AGENT TASK {t.id} ({t.kind}) pending — read {t.prompt_file}")

    def _verify(self, name: str, rtl: List[str]) -> dict:
        r = verification.check(self.verification_mode, self.s["rtl"], rtl, self.s["top"],
                               self.dir / "verify" / name, self.ctx, self.tc, self.design.data["verification"])
        return r.to_dict()

    def phase_direct_eval(self) -> str:
        r = self._direct_round()
        t = self.task_of("direct_rewrite", key=f"round{r}")
        if t is None:
            return "next"
        notes = {}
        try:
            notes = {v.get("file"): v for v in json.loads(Path(t.outputs[-1].path).read_text()).get("variants", [])}
        except Exception:  # noqa: BLE001
            pass
        names = []
        for o in t.outputs[:-1]:
            p = Path(o.path)
            if not p.is_file():
                continue
            name = p.stem
            names.append(name)
            self.s["candidates"].setdefault(name, {"origin": "direct", "rtl": [str(p)], "task": t.id, "round": r,
                                                   "note": notes.get(p.name, {}), "sha256": _sha_file(str(p))})
        # Verify (fail-closed) and get fast post-synthesis deltas for rule proposal (A6);
        # full-PPA measurement is deferred to measure_pending (runs in parallel).
        def work(name: str):
            c = self.s["candidates"][name]
            v = self._verify(name, c["rtl"])
            syn = None
            if v["status"] == "passed":
                r = flow.synthesize(self.ctx, c["rtl"], self.dir / "measure" / name / "synth-preview")
                syn = {q: m.value for q, m in flow.synth_metrics(self.ctx, r).items()}
            return name, v, syn
        with cf.ThreadPoolExecutor(max_workers=max(1, min(len(names), self.tenv.max_parallel))) as ex:
            for name, v, syn in ex.map(work, names):
                c = self.s["candidates"][name]
                c["verification"] = v
                c["verified"] = v["status"] == "passed"
                c["synthesis_preview"] = syn
                self.log(f"direct candidate {name}: verification {v['status']} ({v['detail']})")
                if v["status"] in ("unavailable", "error"):
                    self.s["error"] = f"verification backend {self.verification_mode} {v['status']}: {v['detail']}"
                    return "failed"
        return self._direct_round_decision(r, names)

    def _direct_round_decision(self, r: int, names: List[str]) -> str:
        """Objective-based convergence of the direct-rewrite rounds (A8).

        With more rounds allowed, this round's verified variants are measured
        (full PPA) so the next round can build on the best *weighted objective
        score*; the search stops after ``direct_min_rounds`` once a round does
        not improve the best score. The last round's measurement is deferred
        to run in parallel with the extracted candidates.
        """
        st = self.settings
        max_r, min_r = int(st.get("direct_rounds", 1)), int(st.get("direct_min_rounds", 1))
        rounds = self.s.setdefault("direct_rounds", [])
        if r >= max_r:
            rounds.append({"round": r, "variants": names, "decision": "last round"})
            return "next"
        self._measure_pending()
        b = self.baseline_ppa()
        scores = []
        for n in names:
            c = self.s["candidates"][n]
            if not self._candidate_problems(c):
                ppa = PPA.from_dict(c["measurement"]["ppa"])
                sc = self.objective.score(ppa, b)[0]
                if sc is not None and self.objective.feasible(ppa):
                    scores.append(sc)
        best_round = min(scores) if scores else None
        prev = self.s.get("direct_best_score")
        improved = best_round is not None and (prev is None or best_round < prev)
        if improved:
            self.s["direct_best_score"] = best_round
        rec = {"round": r, "variants": names, "best_score": best_round, "best_so_far": self.s.get("direct_best_score"),
               "improved": improved}
        if r >= min_r and not improved:
            rec["decision"] = "converged (objective did not improve)"
            rounds.append(rec)
            self.log(f"direct rewrites converged after round {r}")
            return "next"
        rec["decision"] = "continue"
        rounds.append(rec)
        self.s["direct_round"] = r + 1
        self.log(f"direct round {r}: best score {best_round}; starting round {r + 1}")
        return "direct_task"

    def _direct_evidence(self) -> str:
        lines = []
        base = Path(self.s["rtl"][0]).read_text().splitlines()
        bp = self.baseline_ppa()
        bsyn = {q: bp.get(q) for q in ("area", "delay")} if bp else {}
        for name, c in self.s["candidates"].items():
            if c.get("origin") != "direct":
                continue
            lines.append(f"### {name}: verification {c.get('verification', {}).get('status')} — "
                         f"{c.get('verification', {}).get('detail', '')}")
            if c.get("note"):
                lines.append(f"Agent note: {c['note'].get('change', '')}")
            prev = c.get("synthesis_preview") or {}
            for q in ("area", "delay"):
                if prev.get(q) is not None and bsyn.get(q):
                    lines.append(f"- post-synthesis {q}: {prev[q]:.6g} vs baseline {bsyn[q]:.6g} "
                                 f"({100 * (prev[q] - bsyn[q]) / bsyn[q]:+.1f}%)")
            cand = Path(c["rtl"][0]).read_text().splitlines()
            diff = list(difflib.unified_diff(base, cand, "original", name, lineterm="", n=1))
            lines.append("```diff\n" + "\n".join(diff[:80]) + "\n```")
        return "\n".join(lines) or "(no direct rewrite candidates)"

    def _pool_rules(self) -> Dict[str, str]:
        rules: Dict[str, str] = {}
        if self.settings.get("seed_rules", True):
            for f in sorted(resources.files("cedar").joinpath("data/rules").iterdir(), key=lambda x: x.name):
                if f.name.endswith(".egg"):
                    rules[normalize_rule_name(f.name)] = f.read_text()
        pool = cfgmod.state_home() / "rule-pool"
        if pool.is_dir():
            for f in sorted(pool.glob("*.egg")):
                rules[normalize_rule_name(f.name)] = f.read_text()
        return rules

    def phase_rules_task(self) -> str:
        n = int(self.settings["rules_to_propose"])
        if n <= 0:
            return "next"
        t = self.task_of("propose_rules")
        if t is None:
            qdir = self.dir / "rules" / "quarantine"
            qdir.mkdir(parents=True, exist_ok=True)
            outs = [TaskOutput(str(qdir / f"{self.s['top']}_rule_{i + 1}.egg"), "egg", required=(i == 0)) for i in range(n)]
            bp = self.baseline_ppa()
            prompt = render(
                "propose_rules", task_id=f"t{len(self.s['tasks']) + 1:03d}-propose_rules", top=self.s["top"], n_rules=n,
                rtl_files=", ".join(self.s["rtl"]), ops_egg=self.s["analysis"]["ops_egg"],
                circt_egg=str(self.tc.template("circt.egg")), widths=self.s["analysis"]["widths"],
                objective=self.objective.describe(), baseline_ppa=bp.summary() if bp else "n/a",
                direct_evidence=self._direct_evidence(), existing_rules=", ".join(sorted(self._pool_rules())),
                output_list="\n".join(f"- `{o.path}`" + ("" if o.required else " (optional)") for o in outs),
                run_dir=str(self.dir))
            t = self.new_task("propose_rules", prompt, outs)
            self._announce(t)
            return AWAIT
        return "next" if t.status == "accepted" else AWAIT

    def phase_rules_admit(self) -> str:
        rules = self._pool_rules()
        generated = []
        t = self.task_of("propose_rules")
        if t is not None:
            for o in t.outputs:
                p = Path(o.path)
                if p.is_file():
                    nm = normalize_rule_name(p.name)
                    rules[nm] = p.read_text()
                    generated.append(nm)
        sigs = certify.signatures_from_template(self.tc.template("circt.egg").read_text())
        store = certify.CertificateStore(cfgmod.state_home() / "certificates", sigs)
        widths = self.s["analysis"]["widths"]
        t0 = time.monotonic()
        adm = certify.admit(rules, widths, store, sigs)
        self.s["rules"] = {
            "generated": generated,
            "admission": adm.summary(),
            "admitted_instances": [{"rule": i.rule_name, "width": i.width, "rule_sha256": i.rule_sha256,
                                    "file": f"{i.rule_name}__w{i.width}.egg",
                                    "certificate": i.certificate.__dict__} for i in adm.admitted],
            # rule sources, kept so a stale certificate can be re-proved from source
            "sources": {n: rules[n] for n in sorted({i.rule_name for i in adm.admitted})},
            "rejected": [c.__dict__ for c in adm.rejected],
            "seconds": round(time.monotonic() - t0, 2),
        }
        idir = self.dir / "rules" / "instances"
        idir.mkdir(parents=True, exist_ok=True)
        for i in adm.admitted:
            (idir / f"{i.rule_name}__w{i.width}.egg").write_text(i.text)
        gen_ok = sorted({i.rule_name for i in adm.admitted if i.rule_name in generated})
        self.log(f"rule admission: {len(adm.admitted)} proved instances, {len(adm.rejected)} rejected; "
                 f"agent rules admitted: {gen_ok or 'none'}")
        return "next"

    def _checked_instances(self, rules: Optional[List[str]] = None) -> List[Tuple[str, int, str]]:
        """Rule instances that may be sent to egglog, as (rule, width, text).

        Only entries of the recorded admission inventory are used, and each one
        must (a) still hash to the certified instance text, (b) carry a PROVED
        certificate from the *current* verifier (otherwise it is re-proved from
        its recorded source), and (c) apply to a width of this design. Edited,
        added or unprovable instance files stop the run; disk contents are never
        used unchecked.
        """
        inv = self.s.get("rules", {}).get("admitted_instances", [])
        idir = self.dir / "rules" / "instances"
        expected = {e.get("file") or f"{e['rule']}__w{e['width']}.egg" for e in inv}
        extra = sorted(p.name for p in idir.glob("*") if p.name not in expected) if idir.is_dir() else []
        if extra:
            raise WorkflowError(f"unrecorded rule instance file(s) {extra} in {idir}; refusing to apply them")
        widths = set(self.s["analysis"]["widths"])
        current = certify.verifier_hash()
        sigs = None
        out: List[Tuple[str, int, str]] = []
        changed = False
        for e in inv:
            if rules is not None and e["rule"] not in rules:
                continue
            fname = e.get("file") or f"{e['rule']}__w{e['width']}.egg"
            cert = e["certificate"]
            if cert.get("status") != "PROVED" or cert.get("width") != e["width"] or e["width"] not in widths:
                raise WorkflowError(f"rule instance {fname}: no PROVED certificate for a width of this design")
            if e["width"] > min(certify.DEFAULT_MAX_WIDTH, certify.MODEL_MAX_WIDTH):
                raise WorkflowError(f"rule instance {fname}: width {e['width']} is beyond the certified range")
            if cert.get("verifier") != current:
                src = (self.s["rules"].get("sources") or {}).get(e["rule"])
                if src is None or certify.rule_sha(src) != e["rule_sha256"]:
                    raise WorkflowError(f"rule instance {fname}: certificate from an older verifier and no "
                                        "matching rule source to re-prove it; start a new run")
                if sigs is None:
                    sigs = certify.signatures_from_template(self.tc.template("circt.egg").read_text())
                c, inst = certify.prove_instance(e["rule"], src, e["width"], sigs)
                if not c.admitted or inst is None:
                    raise WorkflowError(f"rule instance {fname}: re-certification with the current verifier "
                                        f"gave {c.status}: {c.detail}")
                self.log(f"re-certified {fname} with the current verifier")
                e["certificate"] = c.__dict__
                (idir / fname).write_text(inst)
                cert = e["certificate"]
                changed = True
            path = idir / fname
            text = path.read_text() if path.is_file() else ""
            if certify.rule_sha(text) != cert.get("instance_sha256"):
                raise WorkflowError(f"rule instance {fname} differs from its certified text "
                                    "(edited or missing); refusing to apply it")
            out.append((e["rule"], e["width"], text))
        if changed:
            self.save()
        return out

    def _instances(self, rules: List[str]) -> List[str]:
        return [t for _, _, t in self._checked_instances(rules)]

    def phase_probe(self) -> str:
        """Applicability / new-e-node check: each certified instance alone (A7).

        egglog's run report lists only one rule, so every width-instance is
        probed in its own short saturation run to get exact match counts.
        """
        base_nodes = self.s["analysis"]["n_nodes"]
        probes: Dict[str, dict] = {}
        for rule, width, text in sorted(self._checked_instances(), key=lambda x: (x[0], x[1])):
            w = str(width)
            stem = f"{rule}__w{width}"
            p = probes.setdefault(rule, {"matches": 0, "new_nodes": 0, "per_width": {},
                                         "op_kinds": list(lhs_op_kinds(text))})
            try:
                eq = self._eqsat(f"probe-{stem}", [text], 2)
                m, nn = sum(eq.rule_matches), eq.n_nodes - base_nodes
            except pipeline.PipelineError as exc:
                m, nn = 0, 0
                p["error"] = str(exc)[:300]
            p["per_width"][w] = {"matches": m, "new_nodes": nn}
            p["matches"] += m
            p["new_nodes"] = max(p["new_nodes"], nn)
        self.s["probes"] = probes
        app = sorted(r for r, p in probes.items() if p["matches"] > 0 and p["new_nodes"] > 0)
        self.log(f"applicability probe: {len(app)}/{len(probes)} admitted rules fire on this design: {app}")
        self.s["iteration"] = 0
        return "next"

    # --------------------------------------------------- iteration loop

    def _history(self) -> History:
        return History(cfgmod.state_home() / "history" / "rule_history.jsonl")

    def _features(self) -> Dict[str, int]:
        return design_features(self.s["analysis"]["op_histogram"])

    def _ranked(self):
        probes = [Probe(r, p["matches"], p["new_nodes"], tuple(p["op_kinds"])) for r, p in self.s.get("probes", {}).items()]
        ctx = {"technology": self.tech.data["name"]}
        return rank(probes, self._history(), self._features(), ctx)

    def _frontier(self) -> Frontier:
        mode = self.s["measurement"].get("mode", "full-ppa")
        axes = ("area", "delay") if mode == "synthesis-only" else ("area", "delay", "power")
        fr = Frontier(axes, self.baseline_ppa(), self.objective.require_timing_met)
        for name, c in self.s["candidates"].items():
            # Same validity gate as the contract and the report: verified,
            # unchanged RTL, all stages ok, complete and comparable metrics.
            problems = self._candidate_problems(c)
            if problems:
                fr.rejected.append((name, "; ".join(problems)))
                continue
            fr.add(Point(name, PPA.from_dict(c["measurement"]["ppa"]), c["origin"], True))
        return fr

    def phase_iterate(self) -> str:
        st = self.settings
        it = self.s["iteration"]
        iters = self.s["iterations"]
        if iters and iters[-1].get("stage") not in ("done", "failed"):
            return self._iteration_step(iters[-1])
        pend = self.task_of("stop_decision", key=f"after-{len(iters)}")
        if pend is not None and pend.status != "accepted":
            return AWAIT                                   # never skip a pending stop decision
        successful = sum(1 for x in iters if self._iter_ok(x))
        if it >= st["max_iterations"]:
            return "next"
        if self._should_stop(successful):
            return "next"
        self.s["iteration"] = it + 1
        rec = {"iteration": it + 1, "stage": "select", "started_at": now()}
        iters.append(rec)
        self.save()
        return self._iteration_step(rec)

    def _should_stop(self, successful: int) -> bool:
        st = self.settings
        iters = self.s["iterations"]
        if successful < st["min_iterations"]:
            return False
        if len(self._distinct("cost")) < st["min_cost_configs"] or len(self._distinct("rules")) < st["min_rule_combos"]:
            return False
        stall = 0
        for x in reversed(iters):
            if x.get("frontier_changed"):
                break
            if self._iter_ok(x):
                stall += 1
        if stall < st["stall_window"]:
            return False
        if not st.get("agent_stop"):
            return True
        t = self.task_of("stop_decision", key=f"after-{len(iters)}")
        if t is None or t.status != "accepted":
            return False  # the decide stage creates the task
        try:
            return json.loads(Path(t.outputs[0].path).read_text()).get("decision", "STOP").upper() == "STOP"
        except Exception:  # noqa: BLE001
            return True

    def _axes(self) -> tuple:
        mode = self.s["measurement"].get("mode", "full-ppa")
        return ("area", "delay") if mode == "synthesis-only" else ("area", "delay", "power")

    def _candidate_problems(self, c: dict) -> List[str]:
        """Why a candidate is not a verified, completely and comparably measured point."""
        probs: List[str] = []
        if not c.get("verified"):
            probs.append(f"verification {(c.get('verification') or {}).get('status', 'missing')}")
        if not self._rtl_intact(c):
            probs.append("RTL changed after verification")
        m = c.get("measurement")
        if not m:
            probs.append("not measured")
            return probs
        if m.get("pruned"):
            probs.append("approximate pruning skipped P&R/power (unmeasured)")
        failed = [f"{st['stage']} {st['status']}" for st in m.get("stages", []) if st.get("status") != "ok"]
        if failed:
            probs.append("stage(s) " + ", ".join(failed))
        ppa = PPA.from_dict(m["ppa"])
        probs += ppa.require(self._axes())
        b = self.baseline_ppa()
        if b is not None and c is not self.s["candidates"].get("baseline"):
            from ..metrics import comparable
            probs += [f"not comparable to baseline: {p}" for p in comparable(ppa, b, self._axes())]
        if not m.get("complete"):
            probs.append("measurement marked incomplete")
        return list(dict.fromkeys(probs))

    def _iter_ok(self, x: dict) -> bool:
        """One predicate for budgets, diversity, stagnation and the contract:
        the iteration's candidate passed verification AND was completely and
        comparably measured."""
        if not x.get("verified"):
            return False
        c = self.s["candidates"].get(f"iter{x['iteration']:02d}")
        return c is not None and not self._candidate_problems(c)

    def _distinct(self, what: str) -> set:
        out = set()
        for x in self.s["iterations"]:
            if not self._iter_ok(x):
                continue
            if what == "cost":
                out.add(x.get("cost_config_id"))
            elif x.get("rules"):
                out.add(tuple(sorted(x.get("rules", []))))      # the empty combination does not count
        return out

    def _iteration_step(self, rec: dict) -> str:
        stage = rec["stage"]
        i = rec["iteration"]
        name = f"iter{i:02d}"
        if stage == "select":
            ranked = self._ranked()
            tried = [tuple(sorted(x.get("rules", []))) for x in self.s["iterations"][:-1]]
            weights = self.objective.weights_at(i)
            if self.settings.get("agent_selection"):
                t = self.task_of("select_rules", key=name)
                if t is None:
                    out = self.dir / "tasks" / f"{name}_selection.json"
                    table = "\n".join(f"  - {p.rule}: matches={p.matches}, new_nodes={p.new_nodes}, "
                                      f"ops={list(p.op_kinds)}, history={ev}" for p, ev in ranked) or "  (none)"
                    prompt = render("select_rules", task_id=f"t{len(self.s['tasks']) + 1:03d}-select_rules", iteration=i,
                                    objective=self.objective.describe(i), weights=weights, rule_table=table,
                                    tried=[(x.get("rules"), x.get("cost_config_id")) for x in self.s["iterations"][:-1]],
                                    frontier=self._frontier_text(), recent=self._recent_text(),
                                    output_json=str(out), run_dir=str(self.dir))
                    t = self.new_task("select_rules", prompt, [TaskOutput(str(out), "json")], {"key": name})
                    self._announce(t)
                    return AWAIT
                if t.status != "accepted":
                    return AWAIT
                sel = json.loads(Path(t.outputs[0].path).read_text())
                admitted = {p.rule for p, _ in ranked}
                rules = [normalize_rule_name(r) for r in sel.get("rules", []) if normalize_rule_name(r) in admitted]
                mult = costmodel.validate_multipliers(sel.get("cost_multipliers") or {})
                reason = sel.get("reason", "agent selection")
            else:
                rules = complementary_group(ranked, exclude=tried)
                mult = {}
                reason = "deterministic: applicable rules ranked by contextual history, complementary operator coverage"
            rec.update({"rules": rules, "cost_multipliers": mult, "weights": weights, "selection_reason": reason,
                        "cost_config_id": hashlib.sha256(json.dumps([weights, mult], sort_keys=True).encode()).hexdigest()[:12],
                        "stage": "eqsat_extract"})
            self.log(f"{name}: rules {rules} weights {weights} multipliers {mult or '{}'}")
            self.save()
            return "iterate"
        if stage == "eqsat_extract":
            t0 = time.monotonic()
            try:
                eq = self._eqsat(name, self._instances(rec["rules"]), self.settings.get("eqsat_iterations"))
                rec["eqsat"] = {"n_nodes": eq.n_nodes, "n_classes": eq.n_classes, "rule_matches": eq.rule_matches,
                                "egraph": eq.egraph_json}
                calib = (self.s.get("attribution") or {}).get("calibration")
                rep = costmodel.apply_costs(Path(eq.egraph_json), rec["weights"], self.s["analysis"]["scales"],
                                            rec["cost_multipliers"], calib)
                rec["cost_report"] = rep.to_dict()
                ex = pipeline.extract(self.tc, eq, self.s["top"], self.settings["eqsat_timeout_s"])
                cdir = self.dir / "candidates" / "extracted"
                cdir.mkdir(parents=True, exist_ok=True)
                v = cdir / f"{name}.v"
                shutil.copy(ex.verilog, v)
                pipeline.reparse_check(self.tc, v, self.s["top"], self.dir / "tmp" / f"reparse-{name}")
                rec["extraction_method"] = "circt"
                rec["candidate"] = str(v)
            except pipeline.PipelineError as exc:
                rec["extraction_error"] = str(exc)[:1500]
                self.log(f"{name}: CIRCT extraction failed ({exc}); requesting agent extraction fallback")
                rec["stage"] = "fallback"
                self.save()
                return "iterate"
            finally:
                self.time_phase("eqsat_extract", time.monotonic() - t0)
            rec["stage"] = "check"
            self.save()
            return "iterate"
        if stage == "fallback":
            t = self.task_of("extraction_fallback", key=name)
            out = self.dir / "candidates" / "extracted" / f"{name}_agent.v"
            if t is None:
                prompt = render("extraction_fallback", task_id=f"t{len(self.s['tasks']) + 1:03d}-extraction_fallback",
                                iteration=i, error=rec.get("extraction_error", ""), top=self.s["top"],
                                extracted=str(Path(rec.get("eqsat", {}).get("egraph", "")).with_name(f"{self.s['top']}_extracted_raw.txt")),
                                ops_egg=str(Path(rec.get("eqsat", {}).get("egraph", "")).with_name(f"{self.s['top']}.ops.egg")),
                                rtl_files=", ".join(self.s["rtl"]), output_v=str(out),
                                verification_mode=self.verification_mode, run_dir=str(self.dir))
                out.parent.mkdir(parents=True, exist_ok=True)
                t = self.new_task("extraction_fallback", prompt, [TaskOutput(str(out), "verilog")], {"key": name})
                self._announce(t)
                return AWAIT
            if t.status != "accepted":
                return AWAIT
            rec.update({"extraction_method": "agent_reconstruction", "candidate": str(out), "stage": "check"})
            self.save()
            return "iterate"
        if stage == "check":
            v = self._verify(name, [rec["candidate"]])
            rec["verification"] = v
            rec["verified"] = v["status"] == "passed"
            self.s["candidates"][name] = {"origin": "extracted", "rtl": [rec["candidate"]], "verified": rec["verified"],
                                          "verification": v, "iteration": i, "rules": rec["rules"],
                                          "extraction_method": rec.get("extraction_method"),
                                          "sha256": _sha_file(rec["candidate"])}
            self.log(f"{name}: verification {v['status']} ({v['detail']})")
            if v["status"] in ("unavailable", "error"):
                self.s["error"] = f"verification backend {self.verification_mode} {v['status']}: {v['detail']}"
                return "failed"
            rec["stage"] = "done" if not rec["verified"] else "measure"
            if not rec["verified"]:
                rec["frontier_changed"] = False
            self.save()
            return "iterate"
        if stage == "measure":
            # Measure this candidate together with any verified-but-unmeasured
            # direct candidates (parallel EDA runs; same constraints).
            self._measure_pending()
            fr_before = set(self.s.get("frontier_names", []))
            fr = self._frontier()
            names = set(fr.names())
            rec["frontier_changed"] = names != fr_before
            self.s["frontier_names"] = sorted(names)
            c = self.s["candidates"][name]
            ppa = PPA.from_dict(c["measurement"]["ppa"])
            score, probs = self.objective.score(ppa, self.baseline_ppa(), i)
            bscore, _ = self.objective.score(self.baseline_ppa(), self.baseline_ppa(), i)
            rec["ppa"] = ppa.summary()
            rec["score"] = score
            rec["score_problems"] = probs
            rec["measurement_problems"] = self._candidate_problems(c)
            rec["successful"] = not rec["measurement_problems"]
            if not rec["successful"]:
                self.log(f"{name}: NOT a successful iteration — {'; '.join(rec['measurement_problems'])}")
            if c.get("extraction_method") == "circt" or rec.get("extraction_method") == "circt":
                self._attribute(name, rec)
            if not rec.get("history_recorded"):
                rec["history_recorded"] = True       # saved before appending: never double-counted
                self.save()
                self._record_history(rec, score, bscore, ppa)
            rec["stage"] = "decide"
            self.save()
            return "iterate"
        if stage == "decide":
            st = self.settings
            successful = sum(1 for x in self.s["iterations"] if self._iter_ok(x))
            if st.get("agent_stop") and successful >= st["min_iterations"]:
                stall = 0
                for x in reversed(self.s["iterations"]):
                    if x.get("frontier_changed"):
                        break
                    if self._iter_ok(x):
                        stall += 1
                key = f"after-{len(self.s['iterations'])}"
                if stall >= st["stall_window"] and self.task_of("stop_decision", key=key) is None:
                    out = self.dir / "tasks" / f"stop_{key}.json"
                    prompt = render("stop_decision", task_id=f"t{len(self.s['tasks']) + 1:03d}-stop_decision", stall=stall,
                                    iteration=i, max_iterations=st["max_iterations"],
                                    contract=self._contract(), objective=self.objective.describe(i),
                                    frontier=self._frontier_text(), recent=self._recent_text(),
                                    output_json=str(out), run_dir=str(self.dir))
                    t = self.new_task("stop_decision", prompt, [TaskOutput(str(out), "json")], {"key": key})
                    self._announce(t)
                    rec["stage"] = "done"
                    self.save()
                    return AWAIT
            rec["stage"] = "done"
            rec["finished_at"] = now()
            self.save()
            return "iterate"
        return "iterate"

    def _record_history(self, rec: dict, score: Optional[float], bscore: Optional[float], ppa: PPA) -> None:
        bp = self.baseline_ppa()
        deltas = {}
        for q in ("area", "delay", "power"):
            a, b = ppa.get(q), bp.get(q) if bp else None
            deltas[q] = (a - b) / b if a is not None and b else None
        imp = (bscore - score) / bscore if score is not None and bscore else None
        self._history().append({
            "time": now(), "design": self.design.data["name"], "features": self._features(),
            "widths": self.s["analysis"]["widths"], "technology": self.tech.data["name"], "corner": self.s["corner"],
            "clock_ns": self.design.data["clocks"][0]["period_ns_resolved"], "objective": rec["weights"],
            "rules": rec["rules"], "rule_matches": rec.get("eqsat", {}).get("rule_matches"),
            "probes": {r: self.s["probes"].get(r) for r in rec["rules"]}, "verified": rec.get("verified"),
            "complete": bool(rec.get("successful")),
            "metric_deltas": deltas, "score_improvement": imp, "run": self.s["run_id"],
            "note": "combination-level outcome; individual rule effects are not attributed",
        })

    def _measure_pending(self) -> None:
        pending = [n for n, c in self.s["candidates"].items()
                   if c.get("verified") and not c.get("measurement") and n != "baseline"]
        for n in list(pending):
            if not self._rtl_intact(self.s["candidates"][n]):
                c = self.s["candidates"][n]
                c["verified"] = False
                c["verification"] = {"status": "failed", "detail": "candidate RTL changed after verification"}
                self.log(f"{n}: RTL changed after verification — excluded")
                pending.remove(n)
        if not pending:
            return
        self.log(f"measuring {pending} (parallel)")
        with cf.ThreadPoolExecutor(max_workers=max(1, min(len(pending), self.tenv.max_parallel))) as ex:
            futs = {ex.submit(self._measure, n, self.s["candidates"][n]["rtl"]): n for n in pending}
            for f in cf.as_completed(futs):
                n = futs[f]
                md = f.result()
                self.s["candidates"][n]["measurement"] = md
                self.log(f"{n}: {PPA.from_dict(md['ppa']).summary()}")
        self.save()

    def _rtl_intact(self, c: dict) -> bool:
        sha = c.get("sha256")
        return sha is None or (Path(c["rtl"][0]).is_file() and _sha_file(c["rtl"][0]) == sha)

    def _attribute(self, name: str, rec: dict) -> None:
        """Netlist-to-e-node attribution right after measurement, so the
        calibration feeds the *next* iteration's node costs."""
        from ..attribution import attribute_candidate
        att = self.s.setdefault("attribution", {"per_candidate": {}, "calibration": None})
        if name in att["per_candidate"]:
            return
        try:
            r = attribute_candidate(Path(rec["eqsat"]["egraph"]), self.dir / "measure" / name, self.s["top"])
        except Exception as exc:  # noqa: BLE001
            r = {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
        att["per_candidate"][name] = r
        if r.get("status") == "ok" and r.get("calibration"):
            att["calibration"] = r["calibration"]

    def phase_measure_pending(self) -> str:
        self._measure_pending()
        return "next"

    def _frontier_text(self) -> str:
        fr = self._frontier()
        return "; ".join(f"{p.name} ({p.origin}): {p.ppa.summary()}" for p in fr.points()) or "(empty)"

    def _recent_text(self) -> str:
        return "; ".join(f"iter{x['iteration']}: rules={x.get('rules')} verified={x.get('verified')} "
                         f"successful={self._iter_ok(x)} "
                         f"ppa={x.get('ppa', 'n/a')}" for x in self.s["iterations"][-5:]) or "(none)"

    def _contract(self) -> dict:
        iters = self.s["iterations"]
        return presets.contract_status(self.settings, sum(1 for x in iters if self._iter_ok(x)),
                                       len(self._distinct("cost")), len(self._distinct("rules")), len(iters))

    def phase_attribution(self) -> str:
        """Attribute any measured extracted candidate not yet attributed."""
        for name, c in self.s["candidates"].items():
            if c.get("origin") != "extracted" or not c.get("measurement") or not c.get("verified"):
                continue
            rec = next((x for x in self.s["iterations"] if f"iter{x['iteration']:02d}" == name), None)
            if rec is not None and rec.get("extraction_method") == "circt":
                self._attribute(name, rec)
        self.s.setdefault("attribution", {"per_candidate": {}, "calibration": None})
        return "next"

    def _task_timing(self) -> list:
        out = []
        for tid, t in self.s["tasks"].items():
            out.append({"task": tid, "kind": t["kind"], "agent": t.get("agent"), "attempts": t.get("attempts"),
                        "status": t["status"], "wait_s": _seconds_between(t.get("created_at"), t.get("submitted_at"))})
        return out

    def phase_report(self) -> str:
        fr = self._frontier()
        contract = self._contract()
        b = self.baseline_ppa()
        cands = []
        axes = list(fr.axes)
        for name, c in self.s["candidates"].items():
            ppa = PPA.from_dict(c["measurement"]["ppa"]) if c.get("measurement") else None
            score = self.objective.score(ppa, b)[0] if ppa else None
            reasons = self._candidate_problems(c)
            complete = bool(ppa) and not any(r.startswith(("stage(s)", "not measured", "measurement marked"))
                                             or r.split(":")[0] in axes for r in reasons)
            feasible = bool(ppa) and self.objective.feasible(ppa)
            if ppa and not feasible:
                reasons.append("timing goal not met")
            cands.append({"name": name, "origin": c["origin"], "verified": c.get("verified"),
                          "verification": c.get("verification", {}).get("status"),
                          "measured_complete": complete, "timing_feasible": feasible,
                          "ppa": ppa.summary() if ppa else None, "score": score,
                          "not_accepted_because": reasons,
                          "rtl": [_rel(r, self.dir) for r in c["rtl"]]})
        accepted = [c for c in cands if not c["not_accepted_because"]]
        report = {
            "schema": "cedar/report@1", "run_id": self.s["run_id"], "design": self.design.data["name"],
            "top": self.s["top"], "objective": self.objective.to_dict(),
            "objective_text": self.objective.describe(), "constraints_id": self.s["constraints_id"],
            "clock": self.clock_desc(), "measurement": self.s["measurement"],
            "metric_stages": ({"area": "post-synthesis (external-ppa adapter)",
                               "delay": "post-synthesis (external-ppa adapter)",
                               "power": "post-synthesis (external-ppa adapter)"} if flow.is_external(self.ctx) else
                              {"area": "post-synthesis (DC)", "delay": "post-synthesis (DC)",
                               "power": "post-route PrimeTime with SAIF (SDF gate-level simulation) + SPEF"}),
            "verification_mode": self.verification_mode, "preset": self.settings["preset"],
            "execution_contract": contract,
            "iteration_outcomes": [{"iteration": x["iteration"], "successful": self._iter_ok(x),
                                    "problems": ([] if self._iter_ok(x) else
                                                 (self._candidate_problems(self.s["candidates"][f"iter{x['iteration']:02d}"])
                                                  if f"iter{x['iteration']:02d}" in self.s["candidates"]
                                                  else [x.get("extraction_error") or "no candidate produced"]))}
                                   for x in self.s["iterations"]],
            "frontier": fr.to_dict(),
            "candidates": cands,
            "accepted_candidates": [c["name"] for c in accepted],
            "excluded_candidates": [c["name"] for c in cands if c["not_accepted_because"]],
            "rules": {"generated": self.s["rules"].get("generated"), "admission": self.s["rules"].get("admission")},
            "attribution": {k: {kk: vv for kk, vv in v.items() if kk != "per_enode"}
                            for k, v in (self.s.get("attribution", {}).get("per_candidate") or {}).items()},
            "toolchain": self.s["provenance"]["toolchain"],
            "agent": self.s["provenance"].get("agent"),
            "timings_s": self.s["timings"],
            "agent_tasks": self._task_timing(),
            "wall_clock_s": _seconds_between(self.s["created_at"], now()),
            "budget": {"seconds": (self.s.get("budget") or {}).get("seconds"), "expired": False},
        }
        (self.dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
        self.s["final_status"] = "completed" if contract["status"] == "met" else "incomplete"
        self.log(f"report: {self.dir / 'report.json'} — frontier {fr.names()}; contract {contract['status']}")
        return "next"


def _seconds_between(a: Optional[str], b: Optional[str]) -> Optional[float]:
    if not a or not b:
        return None
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
    except ValueError:
        return None


_SIG_RE = re.compile(r"hw\.module\s+@(\S+)\((.*?)\)\s*(?:attributes|\{)", re.S)


def _module_signature(mlir: str, top: str) -> str:
    for m in _SIG_RE.finditer(mlir):
        if m.group(1) == top:
            return re.sub(r"\s+", " ", re.sub(r"loc\([^)]*\)", "", m.group(2))).strip()
    return ""
