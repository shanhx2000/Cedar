# SPDX-License-Identifier: Apache-2.0
"""``cedar`` command-line interface (shared by both agent hosts and by humans)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

import yaml

from . import __version__
from . import config as cfgmod
from .config.schema import ConfigError

EXIT_OK, EXIT_FAIL, EXIT_AWAIT, EXIT_CONFIG = 0, 1, 3, 2


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
    else:
        print(yaml.safe_dump(obj, sort_keys=False, default_flow_style=False).rstrip())


# ------------------------------------------------------------------ toolchain / doctor

def cmd_toolchain(a) -> int:
    from .toolchain import ToolchainError, resolve
    try:
        tc = resolve(a.toolchain_prefix)
    except ToolchainError as exc:
        print(f"toolchain: NOT AVAILABLE — {exc}")
        return EXIT_FAIL
    info = {"identity": tc.identity(), "share_dir": str(tc.share_dir), "probe": tc.probe()}
    _print(info, a.json)
    if a.smoke:
        import subprocess
        here = Path(__file__).resolve()
        script = None
        for cand in (here.parents[2] / "toolchain" / "scripts" / "smoke_toolchain.py",):
            if cand.is_file():
                script = cand
        if script is None:
            return cmd_example_open(argparse.Namespace(output_dir=a.output_dir, toolchain_prefix=a.toolchain_prefix, json=a.json))
        return subprocess.call([sys.executable, str(script), "--bin", str(tc.bin_dir), "--share", str(tc.share_dir)])
    return EXIT_OK


def cmd_doctor(a) -> int:
    from .eda.runner import ToolEnv
    from .setup import load_validation
    from .toolchain import ToolchainError, resolve

    report: dict = {"cedar_version": __version__, "python": sys.version.split()[0],
                    "config_home": str(cfgmod.config_home()), "state_home": str(cfgmod.state_home())}
    ok = True
    tools = tech = None
    prof_prefix = None
    if a.profile:
        try:
            tools = cfgmod.load_tool_profile(a.profile)
            report["tool_profile"] = {"path": str(tools.path), "schema": "valid", "sha256": tools.sha256[:16]}
            env = ToolEnv(tools)
            caps = {}
            for st in ("synthesis", "place_route", "simulation", "saif", "power", "equivalence"):
                if not env.configured(st):
                    caps[st] = {"configured": False, "available": False}
                    continue
                r = env.which(st)
                caps[st] = {"configured": True, "available": r.ok, "detail": r.detail}
            report["capabilities"] = caps
            pp = (tools.data.get("toolchain") or {}).get("prefix")
            prof_prefix = pp["path"] if pp else None
        except ConfigError as exc:
            report["tool_profile"] = {"schema": "INVALID", "errors": exc.errors}
            ok = False
    if a.technology:
        try:
            tech = cfgmod.load_technology(a.technology)
            report["technology"] = {"path": str(tech.path), "schema": "valid (all referenced files exist)",
                                    "sha256": tech.sha256[:16], "corners": sorted(tech.data["corners"])}
        except ConfigError as exc:
            report["technology"] = {"schema": "INVALID", "errors": exc.errors}
            ok = False
    if tools is not None and tech is not None:
        report["end_to_end_validation"] = load_validation(tools, tech)
    if a.design:
        try:
            d = cfgmod.load_design(a.design)
            report["design"] = {"path": str(d.path), "schema": "valid", "top": d.data["top"],
                                "timing_goal_ns": [c["period_ns_resolved"] for c in d.data["clocks"]],
                                "verification": d.data["verification"]["mode"]}
        except ConfigError as exc:
            report["design"] = {"schema": "INVALID", "errors": exc.errors}
            ok = False
    try:
        tc = resolve(a.toolchain_prefix, prof_prefix)
        ident = tc.identity()
        report["toolchain"] = {"source": ident["source"], "bin_dir": ident["bin_dir"],
                               "version": ident["toolchain_version"], "manifest_match": ident["manifest_match"],
                               "probe": tc.probe()}
    except ToolchainError as exc:
        report["toolchain"] = {"status": "NOT AVAILABLE", "detail": str(exc)}
        ok = False
    import shutil
    report["agents"] = {"claude": shutil.which("claude") or "not found", "codex": shutil.which("codex") or "not found"}
    try:
        import z3
        report["z3"] = z3.get_version_string()
    except ImportError:
        report["z3"] = "MISSING (pip install z3-solver)"
        ok = False
    _print(report, a.json)
    return EXIT_OK if ok else EXIT_FAIL


# ------------------------------------------------------------------ setup / profiles

def cmd_setup(a) -> int:
    from . import setup as S
    if a.setup_cmd == "questions":
        print(S.questions_text())
        return EXIT_OK
    if a.setup_cmd == "discover":
        _print(S.discover(), a.json)
        return EXIT_OK
    if a.setup_cmd == "write-profile":
        try:
            p = S.write_profile(a.kind, Path(a.answers), a.name, a.force,
                                Path(a.dest) if a.dest else None)
        except ConfigError as exc:
            print("Profile NOT saved. Problems:")
            for e in exc.errors:
                print(f"  - {e}")
            return EXIT_CONFIG
        print(f"saved {a.kind} profile: {p}")
        return EXIT_OK
    if a.setup_cmd == "schema":
        from .config import specs
        from .config.schema import to_json_schema
        spec = {"tools": specs.TOOL_PROFILE, "technology": specs.TECHNOLOGY, "design": specs.DESIGN}[a.kind]
        print(json.dumps(to_json_schema(spec), indent=2))
        return EXIT_OK
    return EXIT_CONFIG


def cmd_profile(a) -> int:
    if a.profile_cmd == "list":
        out = {}
        for sub in ("profiles", "technologies"):
            d = cfgmod.config_home() / sub
            out[sub] = sorted(p.stem for p in d.glob("*.y*ml")) if d.is_dir() else []
        _print(out, a.json)
        return EXIT_OK
    loader = {"tools": cfgmod.load_tool_profile, "technology": cfgmod.load_technology,
              "design": cfgmod.load_design}[a.kind]
    try:
        ld = loader(a.path)
    except ConfigError as exc:
        print(f"INVALID {a.kind} configuration:")
        for e in exc.errors:
            print(f"  - {e}")
        return EXIT_CONFIG
    print(f"valid {a.kind} configuration: {ld.path} (sha256 {ld.sha256[:16]})")
    return EXIT_OK


def cmd_design_init(a) -> int:
    clocks = [{"port": a.clock_port, "name": a.clock_name}]
    if (a.period_ns is None) == (a.frequency is None):
        print("Specify the timing goal with exactly one of --period-ns or --frequency (e.g. '1.25 GHz').")
        return EXIT_CONFIG
    if a.period_ns is not None:
        clocks[0]["period_ns"] = a.period_ns
    else:
        clocks[0]["frequency"] = a.frequency
    if not a.clock_port:
        clocks[0].pop("port")
    out = Path(a.output).resolve()
    base = out.parent

    def rel(p: str) -> str:
        return os.path.relpath(Path(p).resolve(), base)
    doc = {
        "schema": "cedar/design@1", "name": a.name or a.top, "top": a.top,
        "rtl": [rel(p) for p in a.rtl],
        "profiles": {"tools": a.tools, "technology": a.technology},
        "clocks": clocks,
        "resets": [{"port": r.split(":")[0], "active": (r.split(":") + ["high"])[1],
                    "synchronous": (r.split(":") + ["high", "sync"])[2] != "async"} for r in a.reset],
        "activity": ({"mode": "testbench", "testbench": rel(a.activity_tb), "tb_top": a.tb_top,
                      "dut_instance": a.dut_instance} if a.activity_tb else {"mode": "generated", "cycles": 1000}),
        "verification": {"mode": a.verification},
        "objective": ({"weights": dict(zip(("area", "delay", "power"), (float(x) for x in a.weights.split(","))))}
                      if a.weights else {"preset": a.objective_preset}),
        "measurement": {"mode": "full-ppa", "approximate_pruning": False},
        "search": {"preset": a.preset},
    }
    if a.check_tb:
        doc["verification"]["testbench"] = rel(a.check_tb)
    if out.exists() and not a.force:
        print(f"{out} exists (use --force)")
        return EXIT_CONFIG
    out.write_text(yaml.safe_dump(doc, sort_keys=False))
    try:
        d = cfgmod.load_design(str(out))
    except ConfigError as exc:
        print(f"wrote {out}, but it does not validate yet:")
        for e in exc.errors:
            print(f"  - {e}")
        return EXIT_CONFIG
    from .objective import Objective
    obj = Objective.from_config(d.data["objective"])
    print(f"wrote {out}\n  timing goal: {d.data['clocks'][0]['period_ns_resolved']} ns\n  {obj.describe()}")
    return EXIT_OK


# ------------------------------------------------------------------ flow check

def cmd_flow_check(a) -> int:
    """Small explicit end-to-end check: synth -> P&R -> activity -> power -> equivalence."""
    from .eda import flow
    from .eda.runner import ToolEnv
    from .measure import measure
    from .setup import save_validation
    from . import verification
    d = cfgmod.load_design(a.design)
    tools = cfgmod.load_tool_profile(a.profile or d.data["profiles"]["tools"], d.path.parent)
    tech = cfgmod.load_technology(a.technology or d.data["profiles"]["technology"], d.path.parent)
    corner = d.data["profiles"].get("corner") or tech.data["default_corner"]
    ctx = flow.FlowContext(ToolEnv(tools), tech, corner, d.data["top"], d.data["clocks"], d.data["io_delay_fraction"],
                           d.data["sdc"]["path"] if d.data.get("sdc") else None, dict(d.data.get("defines") or {}),
                           [i["path"] for i in d.data.get("include_dirs") or []])
    out = Path(a.output_dir).resolve() / f"flow-check-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True)
    rtl = [f["path"] for f in d.data["rtl"]]
    act = d.data["activity"]
    if act["mode"] != "testbench":
        print("flow-check needs an activity testbench in the design spec")
        return EXIT_CONFIG
    t0 = time.monotonic()
    m = measure(ctx, rtl, out / "measure", {"testbench": act["testbench"]["path"], "tb_top": act["tb_top"],
                                             "dut_instance": act["dut_instance"]})
    v = verification.check(d.data["verification"]["mode"], rtl, rtl, d.data["top"], out / "verify", ctx, None,
                           d.data["verification"])
    stages = [s.to_dict() for s in m.stages] + [{"stage": "equivalence(self)", "status": "ok" if v.passed else v.status,
                                                 "seconds": v.seconds, "detail": v.detail}]
    passed = m.complete and v.passed and not m.ppa.require(["area", "delay", "power"])
    rec = {"passed": passed, "design": d.data["name"], "ppa": m.ppa.summary(), "info": m.info,
           "stages": [{k: s[k] for k in ("stage", "status", "seconds")} for s in stages],
           "total_seconds": round(time.monotonic() - t0, 1), "output_dir": str(out)}
    (out / "flow_check.json").write_text(json.dumps(rec, indent=2))
    p = save_validation(tools, tech, rec)
    _print(rec, a.json)
    print(f"validation record: {p}")
    return EXIT_OK if passed else EXIT_FAIL


# ------------------------------------------------------------------ runs and tasks

def _agent_label(a) -> dict:
    return {k: v for k, v in {"host": getattr(a, "agent", None), "model": getattr(a, "model", None)}.items() if v}


def _after_advance(run, status: str) -> int:
    print(f"run status: {status}  ({run.dir})")
    if status == "awaiting_agent":
        t = run.pending_task()
        print(f"\nNEXT: agent task {t.id} ({t.kind})")
        if t.feedback:
            print(f"Previous submission rejected: {t.feedback}")
        print(f"Instructions: {t.prompt_file}\n")
        print(Path(t.prompt_file).read_text())
        print(f"\nAfter writing the outputs: cedar task submit --run {run.dir} {t.id} --agent <host> --model <model>"
              f"\nthen: cedar run continue --run {run.dir}")
        return EXIT_AWAIT
    if status in ("completed",):
        print(f"report: {run.dir / 'report.json'}")
        return EXIT_OK
    if status == "incomplete":
        print(f"report (execution contract incomplete): {run.dir / 'report.json'}")
        return EXIT_OK
    print(f"error: {run.s.get('error')}")
    return EXIT_FAIL


def cmd_run(a) -> int:
    from .workflow.engine import Run, WorkflowError
    try:
        if a.run_cmd == "start":
            agent = _agent_label(a)
            run = Run.create(a.design, a.work_dir, a.profile, a.technology, a.toolchain_prefix, a.preset, a.run_id, agent,
                             run_timeout_s=a.run_timeout_s)
            print(f"created run {run.dir}")
            return _after_advance(run, run.advance(a.agent_cmd, agent or None))
        run = Run(Path(a.run))
        if a.run_cmd == "continue":
            if run.s["status"] == "failed" and a.retry:
                run.s["status"] = "running"
            if run.s["status"] in ("failed", "missing_capability", "infeasible_timing_goal") and not a.retry:
                print(f"run is {run.s['status']}: {run.s.get('error')} (use --retry after fixing the cause)")
                return EXIT_FAIL
            return _after_advance(run, run.advance(a.agent_cmd, _agent_label(a) or None))
        if a.run_cmd == "status":
            s = run.s
            info = {"run": str(run.dir), "status": s["status"], "phase": s["phase"], "iteration": s["iteration"],
                    "error": s.get("error"), "pending_task": (run.pending_task().id if run.pending_task() else None),
                    "budget": {"seconds": (s.get("budget") or {}).get("seconds"),
                               "remaining_s": (round((s.get("budget") or {}).get("deadline_epoch", 0) - time.time())
                                               if s.get("budget") else None),
                               "expired": s.get("expired")},
                    "frontier": s.get("frontier_names"), "timings_s": s["timings"],
                    "candidates": {k: {"origin": v["origin"], "verified": v.get("verified"),
                                       "measured": bool(v.get("measurement"))} for k, v in s["candidates"].items()}}
            _print(info, a.json)
            return EXIT_OK
    except (WorkflowError, ConfigError) as exc:
        print(f"error: {exc}")
        return EXIT_CONFIG
    return EXIT_CONFIG


def cmd_run_wait(a) -> int:
    """Block until the run needs the agent, finishes, or fails (or --timeout).

    ``--timeout`` only bounds this *polling* command; the run's execution is
    bounded by its own persisted budget (``search.run_timeout_s``).
    """
    import fcntl
    run_dir = Path(a.run)
    state = run_dir / "run.json"
    deadline = time.monotonic() + a.timeout
    while True:
        busy = False
        lock = run_dir / ".lock"
        if lock.exists():
            with open(lock, "a") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fh, fcntl.LOCK_UN)
                except OSError:
                    busy = True
        status = json.loads(state.read_text()).get("status") if state.is_file() else "missing"
        if not busy and status != "running":
            print(f"run status: {status}")
            if status == "awaiting_agent":
                from .workflow.engine import Run
                t = Run(run_dir).pending_task()
                if t is not None:
                    print(f"pending task {t.id} ({t.kind}): {t.prompt_file}")
                return EXIT_AWAIT
            return EXIT_OK if status in ("completed", "incomplete") else EXIT_FAIL
        if not busy and status == "running" and state.is_file():
            # Nobody is advancing the run (e.g. the start command was killed).
            print("run status: running, but no process is advancing it; resume with `cedar run continue`")
            return EXIT_FAIL
        if time.monotonic() > deadline:
            print(f"still running after {a.timeout}s (status {status}); call `cedar run wait` again")
            return 4
        time.sleep(5)


def cmd_task(a) -> int:
    from .workflow.engine import Run
    run = Run(Path(a.run))
    if a.task_cmd == "show":
        t = run.pending_task() if not a.task_id else None
        tid = a.task_id or (t.id if t else None)
        if not tid:
            print("no pending task")
            return EXIT_OK
        td = run.s["tasks"][tid]
        print(json.dumps({k: v for k, v in td.items() if k != "meta"}, indent=2))
        print(Path(td["prompt_file"]).read_text())
        return EXIT_OK
    ok, msg = run.submit(a.task_id, _agent_label(a))
    print(f"task {a.task_id}: {'ACCEPTED' if ok else 'REJECTED — ' + msg}")
    if ok:
        print(f"next: cedar run continue --run {run.dir}")
    return EXIT_OK if ok else EXIT_FAIL


# ------------------------------------------------------------------ rules

def cmd_rules(a) -> int:
    from .rules import certify
    from .toolchain import ToolchainError, resolve
    try:
        tmpl = resolve(a.toolchain_prefix).template("circt.egg").read_text()
    except ToolchainError:
        tmpl = None
    sigs = certify.signatures_from_template(tmpl)
    store = certify.CertificateStore(cfgmod.state_home() / "certificates", sigs) if not a.no_cache else None
    rules = {Path(f).stem: Path(f).read_text() for f in a.files}
    widths = [int(w) for w in a.widths.split(",")]
    adm = certify.admit(rules, widths, store, sigs, a.timeout_ms, a.max_width)
    _print(adm.summary(), a.json)
    return EXIT_OK if adm.admitted and not adm.rejected else EXIT_FAIL


# ------------------------------------------------------------------ open example

def cmd_example_open(a) -> int:
    """RTL -> MLIR -> e-graph -> weighted extraction -> RTL -> circt-lec; no commercial tools."""
    from importlib import resources
    from .egraph import costmodel, pipeline
    from .rules import certify
    from .toolchain import resolve
    from . import verification
    tc = resolve(a.toolchain_prefix)
    out = Path(a.output_dir).resolve() / f"open-example-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True)
    src = resources.files("cedar").joinpath("data/examples/dot3.v")
    rtl = out / "dot3.v"
    rtl.write_text(src.read_text())
    top = "dot3"
    t0 = time.monotonic()
    analysis = pipeline.eqsat(tc, out / "analysis", top, [str(rtl)], top, [], 1)
    sigs = certify.signatures_from_template(tc.template("circt.egg").read_text())
    rules = {}
    for f in resources.files("cedar").joinpath("data/rules").iterdir():
        if f.name.endswith(".egg"):
            rules[f.name[:-4]] = f.read_text()
    adm = certify.admit(rules, analysis.widths, None, sigs)
    eq = pipeline.eqsat(tc, out / "eqsat", top, [str(rtl)], top, [i.text for i in adm.admitted], 6)
    scales = costmodel.design_scales(json.loads(Path(analysis.egraph_json).read_text()))
    results = {}
    for label, w in (("area", {"area": 1.0, "delay": 0.0, "power": 0.0}),
                     ("delay", {"area": 0.0, "delay": 1.0, "power": 0.0})):
        wd = out / f"extract-{label}"
        import shutil
        shutil.copytree(eq.work_dir, wd)
        eqw = pipeline.EqsatResult(**{**eq.to_dict(), "work_dir": str(wd), "mlir": str(wd / Path(eq.mlir).name),
                                      "egg_program": str(wd / Path(eq.egg_program).name),
                                      "egraph_json": str(wd / Path(eq.egraph_json).name), "steps": []})
        costmodel.apply_costs(Path(eqw.egraph_json), w, scales)
        ex = pipeline.extract(tc, eqw, top)
        v = verification.check("circt-lec", [str(rtl)], [ex.verilog], top, wd / "lec", toolchain=tc)
        code = Path(ex.verilog).read_text()
        results[label] = {"verilog": ex.verilog, "multipliers": code.count("*"), "lec": v.status, "lec_detail": v.detail}
    rec = {"design": str(rtl), "admitted_rule_instances": [f"{i.rule_name}@{i.width}" for i in adm.admitted],
           "egraph_nodes": eq.n_nodes, "rule_matches": eq.rule_matches, "results": results,
           "seconds": round(time.monotonic() - t0, 2),
           "passed": all(r["lec"] == "passed" for r in results.values())}
    (out / "result.json").write_text(json.dumps(rec, indent=2))
    _print(rec, getattr(a, "json", False))
    return EXIT_OK if rec["passed"] else EXIT_FAIL


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cedar", description=__doc__)
    p.add_argument("--version", action="version", version=f"cedar {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, tc=True, js=True):
        if tc:
            sp.add_argument("--toolchain-prefix", help="explicit toolchain prefix (bin/ + share/cedar/)")
        if js:
            sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("toolchain", help="show/probe the selected compiler toolchain")
    common(sp)
    sp.add_argument("--smoke", action="store_true", help="run the RTL->e-graph->RTL smoke test")
    sp.add_argument("--output-dir", default=".")
    sp.set_defaults(fn=cmd_toolchain)

    sp = sub.add_parser("doctor", help="capability diagnostics (configured / available / validated)")
    common(sp)
    sp.add_argument("--profile")
    sp.add_argument("--technology")
    sp.add_argument("--design")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("setup", help="guided setup backend")
    ss = sp.add_subparsers(dest="setup_cmd", required=True)
    q = ss.add_parser("questions")
    q.set_defaults(fn=cmd_setup)
    q = ss.add_parser("discover")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_setup)
    q = ss.add_parser("write-profile")
    q.add_argument("--kind", choices=("tools", "technology"), required=True)
    q.add_argument("--answers", required=True)
    q.add_argument("--name")
    q.add_argument("--dest", help="directory to save into (default: config home)")
    q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_setup)
    q = ss.add_parser("schema")
    q.add_argument("--kind", choices=("tools", "technology", "design"), required=True)
    q.set_defaults(fn=cmd_setup)

    sp = sub.add_parser("profile", help="list/validate configuration files")
    ps = sp.add_subparsers(dest="profile_cmd", required=True)
    q = ps.add_parser("list")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_profile)
    q = ps.add_parser("validate")
    q.add_argument("--kind", choices=("tools", "technology", "design"), required=True)
    q.add_argument("path")
    q.set_defaults(fn=cmd_profile)

    sp = sub.add_parser("design", help="design specifications")
    ds = sp.add_subparsers(dest="design_cmd", required=True)
    q = ds.add_parser("init", help="write a design spec (asks for an explicit timing goal)")
    q.add_argument("--top", required=True)
    q.add_argument("--name")
    q.add_argument("--rtl", nargs="+", required=True)
    q.add_argument("--clock-port", default="clk")
    q.add_argument("--clock-name", default="clk")
    q.add_argument("--period-ns", type=float)
    q.add_argument("--frequency")
    q.add_argument("--reset", action="append", default=[], help="port[:high|low[:sync|async]]")
    q.add_argument("--activity-tb")
    q.add_argument("--tb-top", default="Top")
    q.add_argument("--dut-instance", default="DUT")
    q.add_argument("--verification", choices=("conformal", "simulation", "circt-lec", "external"), required=True)
    q.add_argument("--check-tb", help="self-checking testbench (simulation verification)")
    q.add_argument("--objective-preset", default="balanced")
    q.add_argument("--weights", help="area,delay,power weights, e.g. 0.3,0.2,0.5")
    q.add_argument("--preset", choices=("quick", "paper"), default="quick")
    q.add_argument("--tools", required=True, help="tool profile name or path")
    q.add_argument("--technology", required=True, help="technology profile name or path")
    q.add_argument("-o", "--output", required=True)
    q.add_argument("--force", action="store_true")
    q.set_defaults(fn=cmd_design_init)

    sp = sub.add_parser("flow-check", help="explicit small end-to-end EDA flow validation")
    sp.add_argument("--design", required=True)
    sp.add_argument("--profile")
    sp.add_argument("--technology")
    sp.add_argument("--output-dir", required=True)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_flow_check)

    sp = sub.add_parser("run", help="optimization runs")
    rs = sp.add_subparsers(dest="run_cmd", required=True)
    q = rs.add_parser("start")
    q.add_argument("--design", required=True)
    q.add_argument("--work-dir", required=True, help="parent directory for run output (outside the repo)")
    q.add_argument("--profile", help="tool profile (overrides the design spec)")
    q.add_argument("--technology", help="technology profile (overrides the design spec)")
    q.add_argument("--preset", choices=("quick", "paper"))
    q.add_argument("--run-id")
    q.add_argument("--toolchain-prefix")
    q.add_argument("--agent", help="host running the agent (claude|codex), recorded in provenance")
    q.add_argument("--model", help="model identity, recorded in provenance")
    q.add_argument("--agent-cmd", help="unattended adapter command (e.g. 'claude -p'); default: stop for the active agent")
    q.add_argument("--run-timeout-s", type=int,
                   help="overall wall-clock budget of the run in seconds (default: search.run_timeout_s / preset); "
                        "on expiry running tools are stopped and the run fails")
    q.set_defaults(fn=cmd_run)
    for name in ("continue", "status"):
        q = rs.add_parser(name)
        q.add_argument("--run", required=True)
        q.add_argument("--json", action="store_true")
        if name == "continue":
            q.add_argument("--agent-cmd")
            q.add_argument("--agent")
            q.add_argument("--model")
            q.add_argument("--retry", action="store_true")
        q.set_defaults(fn=cmd_run)

    q = rs.add_parser("wait", help="block until the run needs the agent, finishes or fails")
    q.add_argument("--run", required=True)
    q.add_argument("--timeout", type=int, default=540, help="seconds to poll (default 540); execution itself is bounded by the run budget")
    q.set_defaults(fn=cmd_run_wait)

    sp = sub.add_parser("task", help="agent task contracts")
    ts = sp.add_subparsers(dest="task_cmd", required=True)
    q = ts.add_parser("show")
    q.add_argument("--run", required=True)
    q.add_argument("task_id", nargs="?")
    q.set_defaults(fn=cmd_task)
    q = ts.add_parser("submit")
    q.add_argument("--run", required=True)
    q.add_argument("task_id")
    q.add_argument("--agent")
    q.add_argument("--model")
    q.set_defaults(fn=cmd_task)

    sp = sub.add_parser("rules", help="rule certification")
    rs2 = sp.add_subparsers(dest="rules_cmd", required=True)
    q = rs2.add_parser("certify", help="prove rule instances at exact widths")
    q.add_argument("files", nargs="+")
    q.add_argument("--widths", required=True, help="comma-separated bit widths, e.g. 8,16")
    q.add_argument("--timeout-ms", type=int, default=60000)
    q.add_argument("--max-width", type=int, default=256,
                   help="largest width to certify (default 256); widths above the proof model limit "
                        "(2^20) are always unsupported")
    q.add_argument("--no-cache", action="store_true")
    common(q)
    q.set_defaults(fn=cmd_rules)

    sp = sub.add_parser("example", help="bundled examples")
    es = sp.add_subparsers(dest="example_cmd", required=True)
    q = es.add_parser("open", help="open-source compiler/e-graph example (no commercial EDA tools)")
    q.add_argument("--output-dir", required=True)
    common(q)
    q.set_defaults(fn=cmd_example_open)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except ConfigError as exc:
        print("configuration error:")
        for e in exc.errors:
            print(f"  - {e}")
        return EXIT_CONFIG
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
