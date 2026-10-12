# Checking a converted model against its legacy original

The converter translates a v0.6 model into v0.7; it never runs one. Whether the two versions give the same answer is
checked on **runs you produce**, with two optional tools:

- the `compare` rule: compares the results of a v0.6 run and a v0.7 run of the same case;
- the `fixed_design` rule: a stronger, more expensive check of whether the v0.6 solution is also optimal in v0.7.

Both are optional and do nothing unless you provide the runs. Differences between two runs have two possible causes:

1. **A conversion difference**: a parameter, constraint or cost that means something else in v0.7. The converter
   should bridge these (`docs/conventions.md`, section 16).
2. **A non-unique optimum**: several solutions cost the same, and the two solvers (or versions) pick different ones.
   Capacities without a capacity cost, or the same capacity placed in another region, are typical.

The checks below tell the two apart.

![Consistency checks on the tested cases](figures/consistency_cases.png)

*Tested cases, all solved with CBC: `ERS_test_example` over a full year resampled to 730 h, and the two Calliope
v0.6.10 example models (`national_scale`, `urban_scale`). A: total cost gap of the v0.7 run (filled) and of the v0.7
run with the v0.6 capacities fixed (hollow), far below 0.1% in every case. B: share of capacities (solid) and flows
(dashed) within a given error, per case. C: weighted error per node and summed over nodes; most of it disappears
when summed (the same solution allocated differently across regions), except urban_scale's heat pipes, which carry
heat both ways in equal-cost ways. D-F: one plot per case, capacities (circles) and total flows (triangles) against
the 1:1 line, each scaled to the largest value of its kind, coloured by the divergence from 1:1 on one shared scale;
hollow markers are free capacities. Rebuild with
`pixi run consistency-figure` (`tools/make_consistency_figure.py`).*

## 1. Produce the runs

Put one folder per case in `resources/user/<model>/comparison/<case>/`:

| file | content |
|---|---|
| `v06.nc` | the legacy model solved with Calliope v0.6 (with its `run.py` math): `model.to_netcdf(...)` |
| `v07.nc` | the converted model solved with Calliope v0.7, e.g. with the generated run script: `model.to_netcdf(...)` |

Use the same scenario, time window, time resolution and solver in both. A case can be a short window or a resampled
year (e.g. `config.init.resample.timesteps: 730h`), but beware of annual constraints: on a one-week window an
annual limit, a fuel balance over the horizon or a capacity factor does not mean the same as on a full year, in
either version.

## 2. Compare the runs (`compare` rule)

```bash
pixi run snakemake --cores 1 -s workflow/Snakefile results/<model>/comparison/<case>/comparison.md
```

`comparison.md`, `comparison.png` and one CSV per section are written next to each other. Read them in this order:

1. **Total cost** (first section). If the costs differ by more than about 0.1%, the versions do not have the same
   optimum: look for a conversion difference first.
2. **Free capacities.** A capacity without any capacity cost weighted in the objective (`cost_flow_cap`, purchase,
   annual O&M, per distance; `cost_storage_cap` for storage), and not fixed by min == max, can take any value above
   its peak use at the same cost. These rows are marked `free` (hollow markers) and left out of the error figures.
3. **Per node vs summed over nodes.** The weighted error (sum |v0.7 - v0.6| / sum |v0.6|) of capacities and flows
   per node and summed over nodes. A large per-node error with a small summed one means the same solution is
   allocated differently across regions.
4. **The largest gaps** (numbered in the parity plots, listed per section in the report): if the costs match, these
   are candidates for equal-cost alternatives; if not, they point at the conversion difference.

## 3. Fixed-design check (`fixed_design` rule, optional)

If the costs match and the differences look like alternatives, the fixed-design check confirms it. It fixes every
capacity of the converted model to the v0.6 solution and asks: is that design feasible in v0.7, and does it cost the
same?

```bash
# 1. writes fix_v06_design.yaml (and fixed_design.md saying the run is missing)
pixi run snakemake --cores 1 -s workflow/Snakefile results/<model>/comparison/<case>/fixed_design.md
```

2. Solve the converted model with the **same scenario plus this override**, merged after the scenario's overrides
   (e.g. in the generated run script: `override.union(calliope.io.read_rich_yaml("fix_v06_design.yaml"),
   allow_override=True)` before `calliope.read_yaml`), and save the results as `v07_fixed.nc` in the case folder.
3. Re-run the rule (`--forcerun fixed_design`). `fixed_design.md` gives the verdict and the costs per tech:

| verdict | meaning | next step |
|---|---|---|
| **EQUIVALENT** | the v0.6 design is feasible in v0.7 and costs the same (within 0.1%) | the v0.6 solution is also optimal in v0.7: the remaining differences are equal-cost alternatives, not conversion errors |
| **COSTLIER** | feasible, but more expensive in v0.7 | a cost (or a constraint forcing a costlier dispatch) differs: the costs per tech show which techs |
| **CHEAPER** | feasible and cheaper in v0.7 | a v0.6 constraint or cost is missing or looser in v0.7 |
| **INFEASIBLE** | v0.7 cannot reproduce the v0.6 capacities | a constraint differs; bisect the fixed capacities (fix half of them, then a quarter, ...) to find the techs involved |

What the override does: capacities are fixed within a relative slack of 1e-6 (solver round-off, negative
round-off is read as 0); transmission links once per link; multi-carrier techs on their primary carrier. Batteries in
the translated `storage_ev_constraint` get only a lower bound, because their `storage_cap_max` is also a coefficient
of that constraint. Only capacities are fixed; the dispatch stays free.

**Limits.** The check needs one more solve per case. Large models may not be solvable at all on your machine, and a
shortened or resampled case may not be representative because of annual constraints (section 1). It is therefore not
part of any default target: run it on cases you can afford.

## SPORES

The same approach applies to SPORES runs, SPORE by SPORE. Case `ERS_test_example_spores` (ERS_test_example plus the
legacy override `eurospores/spores-pv.yaml`): full year at 730 h, CBC, 10% cost slack, 2 SPORES, minimising
(`pv_min`) and maximising (`pv_max`) the PV capacity through a `pv_score` cost class, with relative-deployment
scoring of the techs with a finite capacity limit.

| direction | SPORE | total cost v0.6 / v0.7 | PV capacity v0.6 / v0.7 |
|---|---|---|---|
| pv_min | 0 (cost optimum) | 81.2921 / 81.2921 | 1.7247 / 1.7247 |
| pv_min | 1, 2 | 89.4213 / 89.4213 | 0 / 0 |
| pv_max | 0 (cost optimum) | 81.2921 / 81.2921 | 1.7247 / 1.7247 |
| pv_max | 1, 2 | 89.4213 / 89.4213 | 7.3126 / 7.3126 |

Both versions spend the whole slack (1.1 x the cost optimum) and reach the same PV capacity in every SPORE. The
scored techs (wind) follow different paths: the two cost optima are equally cheap alternatives (onshore wind 0.35 vs
0.40), and each SPORE's score depends on the previous solution.

Two v0.6 details matter for such a comparison:

- **Scoring.** Calliope 0.6.10 hard-codes integer scoring (`run_spores` calls `_cap_loc_score_default`); v0.7 offers
  `integer`, `relative_deployment`, `random` and `evolving_average`, and its `integer` differs from v0.6's (1000 vs
  100, relative vs absolute threshold). To compare with another algorithm, the v0.6 run needs the same scoring
  patched into its loop (done here for `relative_deployment`: capacity / `energy_cap_max` above 0.1).
- **Objective classes.** v0.6 builds its objective from `run.objective_options.cost_class` only: classes that
  `spores_options.objective_cost_class` adds later (`spores_score`, `pv_score`, `excl_score`, ...) are never in the
  objective unless declared there with weight 0. Without it, the v0.6 SPORES were arbitrary designs within the slack
  (here "minimising" PV raised it). The converter warns about this (`spores_objective_v06`).

## What the checks found in this project

| finding | check | fixed in the converter |
|---|---|---|
| v0.7 resamples custom timeseries with `first` (v0.6: `mean`): EV fleet infeasible in the 730 h case | `compare` (year cost +12.7%), then fixing the v0.6 EV shares | `resample_method: mean` (conventions, section 7) |
| `storage_ev_constraint` applied to batteries outside run.py's set | `compare` (ERS battery never built) | the set filter is read from each run.py (section 11) |
| `om_annual_investment_fraction` applies to the annualised investment in v0.6, the whole one in v0.7 | fixed-design check: **COSTLIER**, every transmission link +18.5% | `math/om_investment_fraction.yaml` (sections 5, 16) |
| invalid node names (`region1-1`), x/y link distances | converting the v0.6 example models | renaming; Euclidean distances (section 2) |
| SPORES classes missing from `objective_options.cost_class` are ignored by v0.6 | SPORES comparison (v0.6 "minimising" PV raised it) | warning `spores_objective_v06` (section 10) |

After these fixes every tested case is **EQUIVALENT**, and the total costs of the free v0.7 runs match v0.6 within
0.002% (ERS week and year: identical to 7 digits).
