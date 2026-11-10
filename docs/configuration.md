# Configuration reference

Cedar uses three versioned YAML layers. Unknown keys are errors, units are
explicit, and relative paths resolve against the directory of the file that
contains them (never the shell's working directory). `${VAR}` references are
expanded at load time; provenance snapshots keep the unexpanded text, and only
environment-variable *names* are recorded. `cedar setup schema --kind
tools|technology|design` prints a JSON Schema for each layer; `cedar setup
questions` prints the field documentation.

## 1. Tool profile — `cedar/tool-profile@1`

```yaml
schema: cedar/tool-profile@1
name: my-site
environment:
  setup_script: /path/to/site-env.sh   # sourced (bash) before every tool call
  env_required: [LM_LICENSE_FILE]      # names only; checked, never recorded
execution:
  nice: 10
  threads: 4                           # per tool invocation
  max_parallel: 2                      # candidates measured/verified concurrently (licenses!)
tools:
  synthesis:   {adapter: synopsys-dc,        executable: dc_shell,  timeout_s: 1800}
  place_route: {adapter: cadence-innovus,    executable: innovus,   timeout_s: 3600}
  simulation:  {adapter: synopsys-vcs,       executable: vcs}
  saif:        {adapter: synopsys-vcd2saif,  executable: vcd2saif}
  power:       {adapter: synopsys-primetime, executable: pt_shell}
  equivalence: {adapter: cadence-conformal,  executable: lec}
toolchain:                             # optional explicit Cedar toolchain prefix
  prefix: /opt/cedar-toolchain-0.1.0
```

Each stage is a *named adapter* with declared inputs and outputs (see
`src/cedar/eda/flow.py`); arbitrary command strings are not accepted. Site
behaviour goes into the setup script and into the technology hooks.

## 2. Technology profile — `cedar/technology@1`

```yaml
schema: cedar/technology@1
name: mypdk-tt
units: {time: ps}                      # library time unit used in SDC and reports
default_corner: tt
corners:
  tt:
    db: ["${PDK}/db/*_TT.db"]
    liberty: ["${PDK}/lib/*_TT.lib"]
    sim_models: ["${PDK}/verilog/*_TT.v"]
    rc_tech: ${PDK}/qrc/qrcTechFile
lef:
  tech: ${PDK}/lef/tech.lef
  cells: [${PDK}/lef/cells.lef]
synthesis:
  compile_command: compile -map_effort high -area_effort high -no_design_rule -power_effort high
  max_transition: 50.0                 # library time units (default: 0.050 ns converted)
  input_transition_max: 60.0           # default 0.060 ns
  input_transition_min: 20.0           # default 0.020 ns
  dont_use: []
  pre_compile_hook: hooks/pre_compile.tcl     # optional Tcl, sourced by DC
place_route:
  site: coreSite
  core_utilization: 0.6
  core_margin: 4.0                     # um
  filler_cells: [FILLER_CELL_A, FILLER_CELL_B]
  max_route_layer: 6
  route_top_layer: 5
  route_bottom_layer: 2
  post_init_hook: hooks/innovus_post_init.tcl
  accepted_errors: []                  # Innovus message IDs known to be harmless for this
                                       # technology (e.g. [IMPTR-2101] for ASAP7's M10 pitch);
                                       # see "Measurement gates" below
equivalence:
  extra_commands: []                   # Conformal dofile commands before 'set system mode lec'
```

### Measurement gates

EDA tools report most command failures as messages and still reach the end of
the script with exit status 0, so Cedar judges every measurement stage on the
tool's own messages and evidence (recorded per stage in `run.json`):

* **DC / PrimeTime** — any `Error:` or `Fatal:` message fails the stage (the
  scripts also set `sh_script_stop_severity E`); this covers the SDC, hooks,
  compile and reports.
* **PrimeTime power** additionally requires the background `read_parasitics`
  summary for the post-route SPEF with `0 error(s)` and annotated nets, at
  least 95% of internal pin-to-pin nets with parasitics, and SAIF activity on
  at least 90% of nets.
* **VCS activity** requires a completed `$sdf_annotate` of the post-route SDF
  on `<tb_top>.<dut_instance>` with zero errors, no aborted annotation or
  `SDF Error`, and annotated instances in `sdfAnnotateInfo`.
* **Innovus** fails on every `**ERROR` and on skipped/failed SDC commands
  (`CTE-*`), and checks the post-route netlist, SPEF and SDF. A technology may
  list specific error IDs in `place_route.accepted_errors`; they are accepted
  only during floorplan, power routing, placement, routing, filler insertion
  and the hooks, and every accepted occurrence is recorded. Errors while
  reading the design and constraints, extracting, writing outputs or timing
  are never accepted.

A failed stage makes the affected metrics unavailable; the baseline is then
rejected and a candidate is excluded from acceptance, the frontier and
successful-iteration credit.

## 3. Design spec — `cedar/design@1`

```yaml
schema: cedar/design@1
name: dist_mac
top: dist_mac
rtl: [rtl/dist_mac.v]
include_dirs: []
defines: {}
profiles: {tools: my-site, technology: mypdk-tt}   # names (config home) or paths
clocks:
  - {port: clk, period_ns: 1.0}        # or frequency: "1.25 GHz"; omit port for a virtual clock
resets:
  - {port: rst, active: high, synchronous: true}
# sdc: constraints.sdc                 # advanced; required for more than one clock
io_delay_fraction: 0.1
activity:                              # switching activity for power ONLY
  mode: testbench                      # or: generated (random stimulus)
  testbench: tb/tb_activity.v
  tb_top: Top
  dut_instance: DUT
verification:
  mode: conformal                      # conformal | simulation | circt-lec
  # testbench: tb/tb_check.v           # simulation mode: self-checking, prints pass_marker
objective:
  preset: balanced                     # balanced | area | delay | power | paper-power | area-delay
  # weights: {area: 0.3, delay: 0.2, power: 0.5}
  normalization: baseline
  require_timing_met: true
  weight_schedule: []                  # [{from_iteration: 5, weights: {...}}]
measurement:
  mode: full-ppa                       # or synthesis-only (area/delay objectives only)
  approximate_pruning: false
search:
  preset: quick                        # or paper; individual fields override the preset
  hierarchical: false                 # true: optimize module bodies without flattening
  opaque_modules: []                   # exact module symbols excluded in hierarchical mode
  run_timeout_s: 3600                  # overall wall-clock budget (quick 3600, paper 172800);
                                       # see "Run budget" in docs/workflow.md
agent:
  stage_models: {}                     # e.g. {proposal: <model>, selection: <model>}
```

`search.hierarchical: true` preserves module definitions and instances during
e-graph optimization. Internal modules, including private children, are
optimized independently; external declarations and `opaque_modules` are kept.
The assembled candidate goes through the usual whole-design correctness and
PPA gates. See [hierarchical optimization](hierarchical-opacity.md).

Validation rules beyond the schema:

* at least one clock with exactly one of `period_ns` / `frequency`; the timing
  goal is never chosen automatically and an infeasible goal is reported
  (`infeasible_timing_goal`), not relaxed;
* more than one clock requires a user `sdc`;
* `verification.mode: simulation` requires a self-checking testbench that is
  different from the activity testbench;
* `defines` and `include_dirs` are applied to every tool (frontend, DC,
  Conformal via Verilog command files, checking simulation);
* an objective preset or explicit weights is required; weights are finite,
  non-negative, and at least one is positive; a positive power weight requires
  `measurement.mode: full-ppa`.

## Constraint consistency

All stages (DC, Innovus, PrimeTime) read the same generated SDC (or your SDC
verbatim). Every run records a `constraints_id` hash of clocks, SDC, I/O delay,
technology profile, corner and measurement mode; frontiers only compare points
with the same `constraints_id`, measurement mode and per-metric stage.

## External (open-source) adapters

Sites without the commercial tools can delegate measurement and checking to
their own programs (for example Yosys + ABC + OpenSTA):

```yaml
tools:
  synthesis:   {adapter: external-ppa,   executable: /path/to/ppa,   args: [...]}
  equivalence: {adapter: external-check, executable: /path/to/check, args: [...]}
```

* `external-ppa` is called as `<exe> [args] --top T --period-ns P --sdc FILE
  --out DIR --threads N [--clock-port PORT] [--include-dir D]... [--define K=V]...
  -- RTL...`; it must exit 0 and write `DIR/metrics.json` with numeric
  `area_um2`, `delay_ns`, `slack_ns`, optionally `power_uW`, `source`, `netlist`.
  All metrics are recorded at stage `post-synthesis`; P&R, SDF activity and
  PrimeTime are not run, so only `synthesis` (and `equivalence`) are required
  stages. `full-ppa` needs `power_uW`. Netlist-to-e-node attribution needs DC
  reports and is unavailable.
* `verification.mode: external` calls `<exe> [args] --top T --work DIR --golden
  RTL... --revised RTL...`; it must exit 0 and write `DIR/verdict.json`
  `{"status": "passed"|"failed"|"error", "detail": ...}`. Anything else is an
  error (fail-closed).

With these adapters the technology profile needs only `units`, `corners.<c>.liberty`
and `default_corner`.
