# CLAUDE.md

Guidance for working on this repository.

## Purpose (requests from the project owner)

- Convert **legacy Calliope v0.6 models** into **Calliope v0.7.0** models, automatically and repeatably.
- Each folder in `resources/user/` is one legacy model with the same structure:
  `create_input.py`, `dual_utils.py`, `run.py` and `model/`.
  - `model/` holds the tech YAMLs, `overrides*` folders and `eurospores/` (locations, links,
    group constraints, timeseries CSVs and the entry `model-*.yaml`). Every imported YAML is read the same way,
    whatever folder it is in.
  - `run.py` holds the custom pyomo math. It is **reported** (every custom constraint the model uses); translated so
    far, **as run.py writes them (translate, never add new constraints)**: the three time-varying limits
    (`math/time_varying_limits.yaml`), the fuel distribution (`math/fuel_distribution.yaml`: imports == exports
    over all nodes and timesteps, per carrier, as run.py), `storage_ev_constraint` (`math/storage_ev.yaml`, run.py
    naming rule; the set's battery-name filter is read from each run.py; unmatched batteries warned), capacity factors
    (`math/capacity_factor.yaml`) and per-month production (`math/flow_out_per_month.yaml`, `months` dimension).
    The legacy `storage_charge_depth` (max state of charge) has `math/storage_charge_depth.yaml`. CHP `cb`/`cv`/p2h
    is a later goal.
  - Discarded inputs: files like `model-2018_.yaml` (config `exclude_entries`).
- Convert input data (tabular formats, timeseries) and the "architecture" (parameter/variable names,
  techs, nodes, links, group constraints) following the v0.7 rules. Calliope source:
  https://github.com/calliope-project/calliope. Map custom math to the math gallery where possible:
  https://calliope.readthedocs.io/en/latest/examples/math_gallery/ (`chp_htp` for `cb`/`cv`,
  `demand_share_per_timestep_decision`, `max_time_varying`, ...).
- Use **Snakemake**, following the **Modelblocks convention** (https://modelblocks.readthedocs.io/),
  built by hand (no copier), pixi environments, conda-forge only.
- Keep the **optimisation outside** the tool. Duals need no porting: v0.7 reports them
  (`config.solve.shadow_prices`: `system_balance` per node/carrier/timestep, and the CO2 cap constraint).
- **Tests** must make sure bad input data cannot break the tool: input problems become warnings in the
  log/report, or a clear `ConversionError`, never a traceback.
- Every CSV is converted, referenced or not. Unreferenced CSVs are logged as warnings. A CSV used only through
  derived CSVs is not also copied as is; positive demand then keeps the legacy file name
  (`__positive` only if the CSV is also used as it is).
- Later: a `validate` rule; translating the rest of the `run.py` math (CHP `cb`/`cv`/p2h); **shadow prices in
  the comparison** (energy balance per node/carrier/timestep and CO2 caps, v0.6 from `dual_utils` CSVs vs v0.7
  `shadow_price_*`). Dropped from `compare` for now (owner's choice): both runs need a solver that returns duals
  (Calliope v0.7 reads none from CBC), and v0.6 `dual_utils` flips the sign of the CO2 cap price.

## Layout

- `workflow/Snakefile`, `workflow/rules/convert.smk` and `workflow/scripts/convert_model.py` (thin entry points).
- `workflow/scripts/converter/`: the converter package.
  - `legacy.py`: v0.6 YAML loading (imports, dotted keys, structure sanitising).
  - `context.py`: tech identity resolved over the base model plus all overrides.
  - `techs.py`: fragment → params, costs, timeseries.
  - `rules.py`: rename tables.
  - `groups.py`: group constraints → math + data.
  - `config.py`: `model`/`run` → `config`.
  - `timeseries.py`: CSV copy, validation and derived CSVs.
  - `builder.py`: orchestration.
  - `writer.py`, `report.py`, `runpy_inspector.py`, `convert.py` (API + CLI).
  - `compare.py`: v0.6 vs v0.7 run comparison (`compare` rule; runs are user inputs in
    `resources/user/<model>/comparison/<case>/`). Free capacities (no capacity cost in the objective, no min == max)
    are excluded from the errors.
  - `fixed_design.py`: optional fixed-design check (`fixed_design` rule, never a default target): writes
    `fix_v06_design.yaml` (v0.6 capacities fixed, slack 1e-6); the user's `v07_fixed.nc` gives the verdict
    EQUIVALENT / COSTLIER / CHEAPER / INFEASIBLE in `fixed_design.md`. How-to and tested cases: `docs/consistency.md`
    (figure rebuilt by `tools/make_consistency_figure.py`, `pixi run consistency-figure`). Structural v0.6/v0.7 differences found this way
    get a converter fix and a row in `docs/conventions.md` section 16.
  - `runscripts.py`: legacy run scripts (`run-*.py`) read statically -> v0.7 run script (settings verbatim,
    overrides by name, merged at runtime with v0.6 "later wins"; nothing added to the model YAML).
  - `math/`: static custom math.
- `tools/`: developer scripts for the documentation figures in `docs/figures/` (`pixi run dag`,
  `pixi run consistency-figure`); not part of the shipped module.
- `workflow/internal/settings.yaml` lists the package files so Snakemake ships them. **Update it when adding files.**
- `tests/fixtures/mini_legacy/`: small v0.6 model exercising every feature.
  `resources/` and `results/` are gitignored (the 2030 model is 2.9 GB).

## Commands

Run Snakemake and the tests only on `ERS_test_example` and the Calliope v0.6.10 example models (`national_scale`,
`urban_scale`, copied from the legacy package; comparison case `base`). Never on `2030_ERS_eurospores`: it is too
big for this machine.

```bash
pixi run test                        # unit + robustness (hypothesis) + end-to-end
pixi run -e calliope test-calliope   # converted models load/build/solve in Calliope v0.7 (HiGHS)
pixi run test-integration            # Modelblocks standard tests
pixi run ruff check workflow tests tools && pixi run ruff format workflow tests tools && pixi run snakefmt workflow
pixi run snakemake --cores 1 -s workflow/Snakefile results/ERS_test_example/conversion_report.md
pixi run export-snakemake-env module # after changing the `module` pixi feature
pixi run snakemake --cores 1 -s workflow/Snakefile results/ERS_test_example/comparison/week_1h/comparison.md
```

## Design decisions (verified against Calliope 0.7.0; keep them)

`docs/conventions.md` is the user-facing list of these conventions. **Update it whenever a convention changes.**

- **Overrides and duplicate keys.** v0.7 overrides combined in one scenario may not set the same key twice, even
  with equal values.
  - Never emit redundant keys in overrides.
  - Declared scenarios with clashes get a merged override (`scenario_<name>_merged`).
- **Data tables activate techs at every node with data.**
  - Techs with tech-level timeseries get `active: false` guards at nodes where v0.6 did not define them.
  - Overrides that add the tech emit `active: true` only when lifting a guard or for a bare listing.
  - `data_definitions` with (nodes, techs) do *not* activate techs.
- **Costs go in data tables, never YAML.** Tables merge element-wise; YAML-vs-YAML replaces the whole indexed
  param, which would drop cost classes. YAML beats tables.
  Exception: `cost_interest_rate` lives in YAML, as in v0.6: on the `base_*` templates (abstract base groups) and
  per tech where set, since `data_definitions` would override every per-tech value. A tech-level value repeats the
  inherited cost classes (YAML replaces the whole indexed param).
- **Conversion techs** have a `flow_cap` per carrier. Capacity params and flow-linked costs
  (`cost_flow_cap`, `cost_om_annual`, `cost_flow_out`/`in`) are indexed on the primary (capacity) carrier.
  v0.6 had one `energy_cap` per tech, an output capacity: **capacity constraints apply to the primary output
  carrier only**. Group capacity sums use `flow_cap` on `carrier_out`; multi-output techs carry a bool lookup
  `primary_carrier_out` (declared in the group math, dropped when there is none) and count that carrier only.
  Transmission capacity is not halved.
- **conversion_plus: only `carrier_ratios` (+ `energy_eff`) are converted**, following the v0.6 docs (AND across
  tiers, OR within a tier) and the math gallery CHP form. Tier 1 goes into native per-carrier efficiencies
  (`flow_out_eff = energy_eff * r_out`, `flow_in_eff = r_in`). Tiers 2/3 get neutral carrier-indexed ratios
  (`carrier_out_2_to_carrier_out_1_ratio[c] = r_c / r_reference`, reference = primary tier-1 carrier) and generated
  `link_*` constraints (one carrier: gallery multiply form; several: `tier_1 == sum(flow / ratio)`; a multi-carrier
  tier 1 is weighted by `flow_*_eff[ref] / flow_*_eff[c]`, so its efficiencies are always written), plus a
  `balance_conversion` override on tier-1 carriers, selected with `[tech, ...] in techs`. Capacity is translated as
  in v0.6 (owner's choice: consistency with the legacy model over v0.7 per-carrier capacities): tier-1 outputs share
  `flow_cap[primary]` (max and min use), and ramping of every carrier refers to it.
- **`om_annual_investment_fraction`** is a fraction of the *annualised* investment in v0.6, of the whole one in v0.7:
  `math/om_investment_fraction.yaml` redefines `cost_operation_fixed` with `cost_investment_annualised`.
- **Missing `lifetime`.** Techs with investment costs and no `lifetime` anywhere get one `missing_lifetime` warning
  listing them: v0.7 cannot annualise their costs and the objective becomes NaN (with no solver error).
- **YAML layout.** Mappings in block style, lists inline (unless they hold mappings), a blank line between top-level keys.
- **Objective weights.** Cost classes not in the v0.6 objective get `objective_cost_weights: 0`
  (v0.7 defaults unlisted classes to 1, e.g. the negative `co2` cost of `demand_co2` made the LP unbounded).
- **Group constraints.** One constraint per group/type/key. Members are selected with
  `where(array, group_<id>__members)` on a (nodes, techs) lookup, and constraints are gated with
  `any(...)` so they are skipped when empty. Member lookups stay explicit even when a group lists every node
  (a literal translation: regions added later in v0.7 are not included automatically).
  Carriers / cost classes are selected with `[key] in carriers|costs` in `where` (no per-group key lookups), and
  the `demand_share_per_timestep_decision` main constraints are one min/max pair whose `where` ORs the groups.
  `contract_group_families` (default on): per-region copies of a group (one `locs` each, same techs and constraint
  types, names equal without the number, defined in one scope only) become one group with values indexed on
  `nodes` and constraints over `nodes`. Same rows; `false` keeps one constraint per legacy group. Family ids are
  `<override>_<family>` (override names are unique). Literal slices fail on absent techs; vectorising over a shared
  group dimension explodes memory.
- **Unweighted sums.** v0.6 group sums are not timestep-weighted, and `carrier_con` values are negative
  (`flow_in` is positive).
- **Paths and units.**
  - Override data-table paths are relative to the **entry** file.
  - Data-table index headers must be `timesteps`.
  - `distance_unit: m` (v0.6 distances were metres). Links between x/y nodes get the Euclidean `distance`.
  - Invalid v0.7 node names (e.g. `region1-1`) are renamed on load (`legacy.v07_name`), also in CSV headers.
- **Timeseries are trimmed** to the legacy `subset_time`. v0.7 loads whole tables before subsetting
  (the 2030 model needed about 20 GB untrimmed).
- **Resampling averages** (v0.6 `mean`; `resource` summed). v0.7 defaults `resample_method` to `first`, so custom
  parameters declare `mean` and other timeseries params get it in `math/resample.yaml`.
- **SPORES.**
  - `spores_score` costs become `spores_tracked: true` + `tracking_parameter`.
  - Other `objective_cost_class` weights (e.g. `excl_score`) go to `math/spores_objective.yaml`.
