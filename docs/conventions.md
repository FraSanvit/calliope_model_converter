# Conversion conventions: Calliope v0.6 → v0.7

This document lists every convention the converter follows when it translates a legacy Calliope v0.6 model
into a Calliope v0.7.0 model. Read it to know what a v0.6 construct became, what was assumed, and what was
deliberately **not** translated. Everything that needs your attention is also written to
`conversion_report.md` (warnings) for each converted model.

Conventions are grouped by topic. "v0.6" and "v0.7" refer to Calliope 0.6.x and 0.7.0.

## 1. General principles

1. **Inputs only.** The converter translates model inputs (YAML, CSV) and generates the custom math needed to keep
   v0.6 behaviour. It never runs an optimisation.
2. **Literal translation.** A v0.6 construct is translated as written, not as it might have been intended.
   Example: a group listing every region keeps an explicit region list; regions added later in v0.7 are not
   included automatically.
3. **No silent guesses.** Where v0.7 is too different to translate automatically, the converter does not invent
   a translation: it keeps what is unambiguous and logs a warning (see section 13).
4. **Bad input never crashes the converter.** Input problems become warnings in the report, or a clear
   `ConversionError`.

## 2. Model structure

| v0.6 | v0.7 |
|---|---|
| `techs` with `essentials` / `constraints` / `costs` and `parent` chains | flat `techs` with `base_tech` and renamed parameters (section 3) |
| `tech_groups`, including extensions of built-in bases (`tech_groups.conversion`, ...) | `templates` (`base_conversion`, `base_supply`, ...); techs refer to them with `template:` |
| `supply_plus` | `supply` + `include_storage: true` when any storage parameter is set |
| `conversion_plus` | `conversion` (section 6) |
| `locations` (incl. comma-separated keys) | `nodes`, coordinates as `latitude` / `longitude` |
| `links` | one transmission tech per link: `<from>_to_<to>_<tech>` with `link_from`, `link_to`, `template: <tech>` |
| `overrides`, `scenarios` | `overrides`, `scenarios` |
| entry `model-*.yaml` | entry `model-*.yaml` (files like `model-2018_.yaml` are never used as entry: `exclude_entries`) |

- **Distances** are in metres in v0.6: the converted model sets `distance_unit: m`.
- **One converted YAML per legacy YAML**, in the same folder layout, so you can compare files side by side.
- **Overrides combined in one scenario may not set the same key twice in v0.7**, even with equal values (v0.6
  allowed it; the later override won). The converter never writes redundant keys, and a declared scenario whose
  overrides still clash gets one merged override, `scenario_<name>_merged`, built with the v0.6 "later wins" rule.
  Ad-hoc combinations (`scenario="a,b"`) can still clash; the report counts the affected pairs.
- **`exists`** → `active` (techs and locations). A group constraint with `exists: false` is skipped (warned).
- **Location coordinates** `lat`/`lon` → `latitude`/`longitude`; `x`/`y` coordinates are not supported in v0.7 and
  are dropped (info). Links between x/y nodes without a `distance` get the Euclidean distance between the
  coordinates, as v0.6 computed it (v0.7 computes lat/lon distances itself). A link-level `distance` is kept.
- **Node names must be valid v0.7 identifiers** (letters, digits, `_`; not starting with a digit or `_`). v0.6 allowed
  e.g. `region1-1` (national-scale example): invalid names are renamed (`region1-1` → `region1_1`, a leading digit
  gets `n`, clashes get `_2`, ...) in locations, links, group `locs` and in the CSV headers of the copied and derived
  timeseries (warning `renamed_nodes` lists them). The `compare` rule maps v0.6 node names the same way.
- **Techs under locations:** a tech_group or a transmission tech listed under a location is ignored (warned; links
  carry transmission techs).
- **Tech definitions:** a tech never defined with a `parent` in this model, but modified in an override, is skipped
  (`undefined_tech`); circular or unknown `parent` chains and empty overrides are warned.
- **Activation.** In v0.7 data tables activate a tech at every node that has data. Techs with tech-level
  timeseries therefore get `active: false` at nodes where v0.6 did not define them; overrides that add the tech
  there set `active: true`.

## 3. Parameters

Plain renames (base-tech independent):

| v0.6 constraint | v0.7 parameter |
|---|---|
| `energy_cap_max` / `_min` | `flow_cap_max` / `flow_cap_min` |
| `energy_cap_max_systemwide` / `_min_systemwide` | `flow_cap_max_systemwide` / `flow_cap_min_systemwide` |
| `energy_cap_min_use` | `flow_out_min_relative` |
| `energy_cap_per_unit` | `flow_cap_per_unit` |
| `energy_cap_per_storage_cap_max` / `_min`, `charge_rate` | `flow_cap_per_storage_cap_max` / `_min` |
| `energy_eff_per_distance` | `flow_out_eff_per_distance` |
| `energy_ramping` | `flow_ramping` |
| `parasitic_eff` | `flow_out_parasitic_eff` |
| `resource_eff` | `source_eff` |
| `resource_cap_max` / `_min` | `source_cap_max` / `source_cap_min` |
| `resource_cap_equals_energy_cap` | `source_cap_equals_flow_cap` |
| `resource_area_max` / `_min`, `resource_area_per_energy_cap` | `area_use_max` / `_min`, `area_use_per_flow_cap` |
| `export_cap` | `export_max` |
| `force_asynchronous_prod_con` | `force_async_flow` |
| `units_max` / `_min` / `_max_systemwide` | `purchased_units_max` / `_min` / `_max_systemwide` (needs the `milp` math; warned) |
| `storage_*`, `lifetime`, `one_way`, `distance` | unchanged |

- **`*_equals` → `*_min` + `*_max`** with the same value (`energy_cap_equals` → `flow_cap_min` and `flow_cap_max`, ...).
- **`energy_eff`** → `flow_out_eff` (supply, conversion, transmission), `flow_in_eff` + `flow_out_eff`
  (storage), `flow_in_eff` (demand). conversion_plus: see section 6.
- **`resource`** → `source_use_max` (supply), or `source_use_equals` when `force_resource: true`;
  demand → `sink_use_max` / `sink_use_equals` (v0.6 demand forces its resource by default), with the **sign
  flipped** (v0.6 demand values are negative, v0.7 sink use is positive). A forced infinite resource becomes an
  upper bound (warned).
- **`resource_unit`** `energy` / `energy_per_cap` / `energy_per_area` → `source_unit` / `sink_unit`
  `absolute` / `per_cap` / `per_area`.
- **`export_carrier`** → `carrier_export`.
- **Dropped** (no information in v0.7): `energy_con`, `energy_prod`, `force_resource` (used to choose
  `*_equals` vs `*_max`).
- **`storage_charge_depth`** (from a Calliope v0.6 branch): kept, with `math/storage_charge_depth.yaml` (added only when
  used): `storage <= storage_charge_depth × storage_cap` in each timestep, the maximum state of charge, mirroring
  v0.7's own `storage_discharge_depth` (minimum). Time-varying values (`file=`) are allowed.
- **Not translated** (warned): `energy_cap_scale`, `resource_scale`, `resource_min_use`, `storage_time_max`.
- **Unknown keys** are copied unchanged and warned.
- **Custom parameters read by `run.py`** keep their values under math-gallery names where one exists:
  `cb` → `power_to_heat_ratio`, `cv` → `power_loss_factor`, `energy_cap_max_time_varying` →
  `flow_cap_max_relative_per_ts`, `energy_cap_equals_time_varying` → `flow_cap_equals_relative_per_ts`,
  `energy_cap_max_time_varying_con` → `flow_in_max_relative_per_ts`, `energy_cap_ratio` → `flow_cap_ratio`,
  `capacity_factor_min/max` (unchanged), `carrier_prod_per_month_*` → `flow_out_per_month_*`.

## 4. Capacity

1. **v0.6 has one capacity per tech, `energy_cap`, and it is an output capacity.** v0.7 has one `flow_cap` per
   carrier of a tech.
2. **Capacity constraints apply to the primary output carrier only.** Capacity bounds
   (`flow_cap_max/min/equals`, `flow_cap_per_unit`, `flow_out_min_relative`, ...) and capacity costs are indexed on
   the primary output carrier (`primary_carrier_out` in v0.6, else the first output carrier).
3. **Group capacity constraints** sum `flow_cap` on output carriers (`carrier_out`). Techs with several output
   carriers carry a bool lookup `primary_carrier_out` and count that carrier only (e.g. a CHP counts its
   electricity capacity, not its heat). Input capacities never count. The lookup is declared in
   `math/group_constraints.yaml` and dropped when a model has no group constraints.
4. **Transmission capacity is not halved.** A link counts with its full capacity.

## 5. Costs

1. **Costs live in data tables**, not YAML: `data_tables/generated/costs.csv` (rows `techs, costs`) and
   `costs_per_carrier.csv` (rows `techs, costs, carriers`), plus `node_costs*.csv` for node-specific costs and one
   set per override under `generated/overrides/<name>/`. Two tables are needed because a data table has one fixed
   set of row dimensions. Tables merge element by element, like v0.6 overrides did (YAML would replace a whole
   indexed parameter and lose cost classes).
2. **Renames:** `energy_cap` → `cost_flow_cap`, `energy_cap_per_distance` → `cost_flow_cap_per_distance`,
   `storage_cap` → `cost_storage_cap`, `resource_cap` → `cost_source_cap`, `resource_area` → `cost_area_use`,
   `om_annual` → `cost_om_annual`, `om_annual_investment_fraction` → `cost_om_annual_investment_fraction`,
   `om_prod` → `cost_flow_out`, `om_con` → `cost_source_use` (supply) / `cost_flow_in` (others),
   `export` → `cost_export`, `purchase` → `cost_purchase`, `interest_rate` → `cost_interest_rate`,
   `depreciation_rate` → `cost_depreciation_rate`.
3. **Carrier-linked costs** (`cost_flow_cap`, `cost_flow_out`, `cost_om_annual`, `cost_export`, ...) are indexed on
   the primary output carrier; `cost_flow_in` on the primary input carrier.
4. **Interest rate is the exception: it lives in YAML.** As in v0.6, the common rate sits on the abstract base
   templates (`base_supply`, `base_conversion`, ... in `interest-rate.yaml`) and techs with their own rate set it
   in their definition, which wins over the template. (A single `data_definitions` value would override every
   per-tech rate.) When a tech sets a rate for one cost class, the classes it inherits are repeated, because a
   tech-level value replaces the template's value as a whole.
5. **Objective weights.** Only the cost classes in the v0.6 objective (`run.objective_options.cost_class`, default
   `monetary: 1`) are optimised. Every other cost class gets `objective_cost_weights: 0`, because v0.7 weights
   unlisted classes by 1 (with the negative `co2` cost of `demand_co2` this made the problem unbounded).
6. **`lifetime` is required with investment costs** (capacity, storage, source, area or purchase costs). Techs that
   have such costs but no `lifetime` anywhere (tech, tech_groups, locations, overrides) are listed in one
   `missing_lifetime` warning (failed check): v0.7 cannot annualise their costs and the objective becomes NaN,
   with no solver error.
7. **`om_annual_investment_fraction` is a fraction of the annualised investment** in v0.6 (`costs.py`:
   `fraction x cost_cap`, with `cost_cap` already depreciated and scaled to the modelled period). v0.7's
   `cost_om_annual_investment_fraction` multiplies the whole (lifetime) investment, 1 / annuity factor times more
   (13.5x for 60 years at 7.3%). When a model uses it, `math/om_investment_fraction.yaml` redefines v0.7's
   `cost_operation_fixed` as `annualisation_weight x sum(cost_om_annual x flow_cap) + cost_investment_annualised x
   cost_om_annual_investment_fraction`, i.e. as v0.6. Found by the fixed-design check (`docs/consistency.md`)
   (ERS_test_example: every transmission link cost 18.5% more with the same capacity).

## 6. Conversion and conversion_plus technologies

1. **Plain conversion** (one input, one output): `energy_eff` → `flow_out_eff`; capacity on the output carrier.
   **A carrier repeated on the same side** (listed twice in `carrier_out`, or in both `carrier_out` and `carrier_out_2`;
   the same for inputs) is kept once, at its first occurrence (tier order `out`, `out_2`, `out_3`); the repeat and its
   carrier ratio are ignored (`duplicate_carrier` warning). The same carrier as input and output is fine (storage,
   transmission, v2g-like conversion techs) and is left unchanged.
2. **conversion_plus: only `carrier_ratios` and `energy_eff` are converted**, with the v0.6 semantics
   (Calliope v0.6.9 docs, *Advanced constraints*: CHP, air source heat pump, CCHP):
   - carriers **across tiers** are combined with **AND** (`carrier_out` + `carrier_out_2`: electricity and heat);
   - carriers **within one tier** are combined with **OR** (any mix: `carrier_out: [heat, cooling]`).
3. **Tier 1 → native v0.7 efficiencies.** `flow_out_eff` per tier-1 output carrier = `energy_eff × ratio`,
   `flow_in_eff` per tier-1 input carrier = its ratio. This reproduces the v0.6 balance
   `Σ out / ratio = Σ in × ratio × energy_eff`. A time-varying ratio (e.g. a heat-pump COP file) becomes a
   time-varying `flow_out_eff`.
4. **Tiers 2 and 3 → neutral ratios and link constraints** in the math gallery CHP form
   (`math/conversion_plus.yaml`):
   - parameters `carrier_out_2_to_carrier_out_1_ratio`, `carrier_out_3_to_carrier_out_1_ratio`,
     `carrier_in_2_to_carrier_in_1_ratio`, `carrier_in_3_to_carrier_in_1_ratio`, indexed on the carriers of the
     tier: value = `ratio_carrier / ratio_reference`, where the reference is the primary tier-1 carrier. Neutral
     names, so one parameter serves every tech;
   - one `link_*` constraint per tier and carrier set. A single carrier in the tier gives the gallery form
     `flow_out[carriers=electricity] * carrier_out_2_to_carrier_out_1_ratio[carriers=heat] == flow_out[carriers=heat]`;
     several carriers (OR) give `flow_out[carriers=electricity] == flow_out[carriers=heat] / ratio[heat] +
     flow_out[carriers=cooling] / ratio[cooling]` (CCHP: one unit of tier 2 per unit of electricity, met by any mix
     of 0.8 heat and 0.5 cooling);
   - `balance_conversion` keeps only the tier-1 carriers of these techs;
   - techs are selected with `[tech, ...] in techs`; techs with the same carriers share one equation.
5. **Several tier-1 carriers with different ratios, linked to tiers 2/3:** in the link, each tier-1 carrier is
   weighted by `ratio_reference / ratio_carrier`, taken from its efficiency
   (`flow_out_eff[reference] / flow_out_eff[carrier]`; `energy_eff` cancels out). The efficiencies of such a
   tier are therefore always written. This reproduces v0.6 `Σ tier-1 flow / ratio = Σ tier-n flow / ratio`
   exactly in every case.
   Verified on the v0.6 docs examples (CHP, air source heat pump, CCHP and the "Complex fictional technology" with
   OR tiers on both sides and three output tiers): the solved v0.7 flows satisfy every v0.6 tier balance
   (tests `test_conversion_plus_or_tiers`, `test_complex_conversion_plus`).
6. **Capacity is translated as in v0.6: one capacity, on the primary output carrier.** This keeps the legacy
   model's behaviour rather than v0.7's per-carrier capacities, so results stay comparable:
   - the tier-1 outputs share `flow_cap[primary]`: `Σ tier-1 flow_out <= flow_cap[primary] × timestep_resolution`
     (`conversion_plus_flow_out_max`, v0.6 `carrier_production_max_conversion_plus`);
   - `energy_cap_min_use` applies to that sum (`conversion_plus_flow_out_min`; the v0.7 per-carrier `flow_out_min`
     is switched off for these techs);
   - ramping of every carrier, inputs included, refers to `flow_cap[primary]` (as v0.6 `energy_ramping × energy_cap`);
   - with a single tier-1 output (the primary) the v0.7 per-carrier constraints are already exact, so only
     ramping is overridden.
   Capacity bounds and costs are on the primary carrier (section 4); the other carriers' `flow_cap` are free and
   unused. Listed in the report (`conversion_plus_capacity`, info).
7. **`carrier_ratios` on a tech_group** are resolved onto each member tech.
8. **A ratio of 0** on a single tier 2/3 carrier forces that flow to zero (warned). Within a tier with several
   carriers v0.6 divides by it, which v0.7 cannot do: that ratio is not converted (warned).

## 7. Timeseries (`file=` references)

1. Every legacy CSV is converted, referenced or not; unreferenced CSVs are warned about. A CSV used only through
   derived files (e.g. demand, made positive) is not copied as well: the derived file is its conversion.
2. CSVs are used as wide `data_tables` (rows `timesteps`, columns `nodes`); the index header is renamed to
   `timesteps`.
3. **Trimmed to the legacy `subset_time`** (union over the base model and all overrides), because v0.7 loads whole
   tables before subsetting. Option `trim_timeseries: false` keeps everything.
4. Values that need a transformation get a derived CSV next to their source (same folder under `data_tables/`).
   Demand made positive keeps the legacy file name (the negative original is then not copied); only if the same
   CSV is also used as it is does the positive one get `__positive`. Other transforms are named after the
   transform: `__invert`, `__x<factor>`, `__<column>`.
5. Paths in overrides are relative to the **entry** file.
6. A referenced CSV that is missing, or has no column for the model's locations, drops that parameter (warned);
   CSVs with empty/NaN values or text are warned about. `df=` references (in-memory dataframes) cannot be converted
   from files and are dropped (warned). Timeseries on transmission techs are not supported and dropped (warned).
7. **Time resampling averages, as in v0.6.** v0.6 resampled every timeseries with `mean`, except `resource` (summed).
   v0.7 uses each parameter's `resample_method`, which defaults to `first` when the math sets none (custom
   parameters, and some base ones such as `flow_out_eff`). The converter's custom parameters declare `mean`, and
   every other timeseries parameter without one gets `mean` in `math/resample.yaml` (`source_use_*`/`sink_use_*`
   keep v0.7's `sum`). Without this, a 730 h run used the first hour of each window: charging limits off by -8% to
   +12% made the v0.6 EV fleet infeasible in v0.7 (year case cost +12.7% instead of +1.1%).
8. **Big files are read by the span, in chunks.** Timesteps are sorted, so the rows of `subset_time` are located by
   bisection on byte offsets (a few dozen reads); validation, the trimmed copy and derived CSVs read only those bytes,
   100 000 rows at a time, so memory does not grow with the file. Validation covers the kept rows (the trimmed-away
   years cannot affect the converted model). Duplicated timesteps make a CSV unusable (warned); unsorted or non-ISO
   timesteps are read in full instead (`csv_unsorted` warning when unsorted). The model entry file is found by a text
   scan for a top-level `model:` line before any YAML is parsed.

## 8. Group constraints

1. **Generated math** in `math/group_constraints.yaml`; values in `data_definitions`, in the base model or in the
   override that defines the group, so each override switches on its own groups.
2. **One constraint per group, type and key** (carrier or cost class). Names:
   `group_<group>__<type>_<min|max>__<key>_constraint`, value parameter without `_constraint`.
3. **`equals` → a `min` and a `max` constraint**; an infinite bound gives no constraint.
4. **Members** are a bool lookup `group_<group>__members` over `(nodes, techs)` (or only the restricted dimension).
   Constraints select them with `where(array, group_<group>__members)`; a group with no active member in a
   scenario is skipped (`any(...)` gate). Member lists stay explicit even if a group lists every region.
5. **Carriers and cost classes** are selected with `[key] in carriers` / `[key] in costs` in `where`.
6. **v0.6 sums are kept unweighted** over timesteps (no `timestep_weights`), and v0.6 `carrier_con` values
   (negative) become positive `flow_in` limits.
7. **Group families** (option `contract_group_families`, default `true`): per-region copies of a group (one location
   each, a different one per copy; same techs and constraint types; defined in one place only and nowhere else) become
   one group, whatever their names (`ALB_1_diesel`, `AUT_2_diesel`, `biofuel_ALB`, ...). If two such groups share a
   location, only copies whose names are equal once the number is removed (`biofuel_1..4`) are merged. The family is
   named after the name parts all copies share (`diesel`), or its constraint type if there are none. Its value is
   indexed on `nodes` and its constraints loop over `nodes`. The rows are identical. `false` keeps one constraint per legacy group. Each merge is listed in the
   report (`group_family`). A family defined in an override is named `<override>_<family>`
   (`biofuel_maximum_biofuel`, `annual_fuel_demand_biofuel`), unless its name already starts with the override name
   (`demand_share_passenger_car_transport`). Override names are unique, so families never clash; a family of the
   base model keeps its short name.
8. **A group redefined in several overrides with different members** becomes one constraint per definition; if
   those overrides are combined, all definitions apply (v0.6 kept the last one). Warned.
9. **Shadow prices**: the CO2 cap constraints (cost class containing `co2`) are listed in
   `config.solve.shadow_prices` next to `system_balance`.

## 9. `demand_share_per_timestep_decision`

1. Adapted from the math gallery; the decision variable is indexed on `(nodes, techs, carriers)` like the v0.6
   variable, so custom math that uses it maps one to one.
2. The main constraints are **one min/max pair for all groups**: their `where` joins the groups with OR (each
   group's members, carrier and value parameter). Relaxation from
   `run.relax_constraint.demand_share_per_timestep_decision_main_constraint` → `demand_share_relaxation`.
3. A finite legacy share adds one constraint per group: the members' shares sum to it. An infinite share (`.inf`)
   adds none.

## 10. Configuration (`model:` / `run:`)

| v0.6 | v0.7 |
|---|---|
| `model.name` | `config.init.name` |
| `model.subset_time` | `config.init.subset.timesteps` (also used to trim the timeseries, section 7) |
| `model.time` with `function: resample` | `config.init.resample.timesteps` (other time functions, e.g. clustering, are not supported: warned) |
| `model.timeseries_dateformat` | `config.init.datetime_format` |
| `model.calliope_version`, `timeseries_data_path` | dropped (not needed); `random_seed`, `file_allowed` dropped (info) |
| `run.mode` `plan` / `operate` / `spores` | `config.init.mode` `base` / `operate` / `spores` |
| `run.solver` (`*_persistent` → plain name, warned), `solver_io`, `zero_threshold`, `save_logs`, `solver_options` | `config.solve.*` |
| `run.ensure_feasibility`, `run.backend` | `config.build.ensure_feasibility`, `config.build.backend` |
| `run.bigM` | `data_definitions.bigM` |
| `run.objective_options.cost_class` | `data_definitions.objective_cost_weights` (unlisted classes get 0, section 5; `spores_score` removed) |
| `run.objective_options.sense: maximize` | warned: use negative `objective_cost_weights` in v0.7 |
| `run.relax_constraint.demand_share_per_timestep_decision_main_constraint` | `data_definitions.demand_share_relaxation` |
| `run.operation.window` / `horizon` | `config.build.operate.window` / `horizon` (`<n>h`, assuming hourly timesteps; info) |
| `run.spores_options.spores_number` / `slack` | `config.solve.spores.number` / `data_definitions.spores_slack` |
| `run.spores_options.save_per_spore(_path)` | `config.solve.spores.save_per_spore_path` (a file pattern becomes its directory) |
| `run.spores_options.score_cost_class` | techs with those costs get `spores_tracked: true` + `config.solve.spores.tracking_parameter` |
| `run.spores_options.slack_cost_group` | replaced by v0.7's own `total_system_cost_max` (info) |
| `run.spores_options.objective_cost_class` | `data_definitions.spores_objective_cost_weights` + `math/spores_objective.yaml` (e.g. `excl_score`). Calliope 0.6 builds its objective from `objective_options.cost_class` only, so SPORES classes not declared there (weight 0) were ignored by the v0.6 run but are applied in v0.7: warned (`spores_objective_v06`) |
| `run.cyclic_storage: false` | per tech in v0.7 (`cyclic_storage: false` on storage techs; warned) |

Unknown `model` / `run` keys are warned and dropped. Every converted model lists `system_balance` and its CO2 cap
constraints in `config.solve.shadow_prices` (section 8).

## 11. Custom math and duals

1. **`run.py` math is reported, and translated constraint by constraint, as `run.py` writes it** (no new
   constraints). Each math file is added only when a model uses its parameters/techs. Translated so far:
   the three time-varying limits (`math/time_varying_limits.yaml`), following the math gallery `max_time_varying`:
   - `energy_cap_max_time_varying` → `max_time_varying_flow_out`: `flow_out <= flow_cap × timestep_resolution ×
     flow_out_parasitic_eff × flow_cap_max_relative_per_ts` (the gallery form; `flow_out_parasitic_eff` is a v0.7
     parameter, default 1, from v0.6 `parasitic_eff`);
   - `energy_cap_equals_time_varying` → `equals_time_varying_flow_out` (same, `==`, `flow_cap_equals_relative_per_ts`);
   - `energy_cap_max_time_varying_con` → `max_time_varying_flow_in` (inflow variant, `flow_in_max_relative_per_ts`;
     no parasitic factor, as in the standard `flow_in_max`).
   Capacity limits never include the conversion efficiency: flows are compared with the capacity directly, as in v0.6.
   As in `run.py`, they replace the standard `flow_out_max` / `flow_in_max` for those techs (the extra math redefines
   only the `where` of those base constraints), so profiles above 1 keep working (2030 heat pumps reach 1.9), and
   flows include `timestep_resolution` (the gallery omits it, which only matters above 1-hourly).
   They apply per carrier on its own `flow_cap`, which is exact for single-carrier techs and for the primary output.
   Conversion techs with several tier-1 outputs (e.g. v2g) or with an inflow limit get per-tech constraints on the
   **sum of tier-1 flows against the primary output's `flow_cap`**, as v0.6 measured them against its one
   `energy_cap` (`*_time_varying_flow_out_sum_*`, `max_time_varying_flow_in_sum_*`). 
   - **Fuel distribution** (`fuel_distribution_constraint`) → `math/fuel_distribution.yaml`, based on the math
     gallery `fuel_dist`: the `<carrier>_distribution_import` (supply) and `<carrier>_distribution_export` (demand)
     techs are kept and tagged with the lookups `fuel_distribution_import` / `fuel_distribution_export`; the
     constraint `fuel_distribution_balance` (the form of the gallery's `restrict_total_imports_and_exports`) makes,
     **per carrier, over the whole horizon** (as `run.py`), the imports over all nodes and timesteps equal the
     exports (unweighted sums, as `run.py`). Fuel can therefore shift in time, as in v0.6.
     Pairs are found by the `run.py` naming rule; an import without its export is warned (`fuel_distribution`).
   - **EV battery capacity** (`storage_ev_constraint`) → `math/storage_ev.yaml`, one constraint per pair:
     `storage_cap` of `<sector>_<mode>_battery` = `demand_share_per_timestep_decision` of
     `<sector>_transport_<mode>` (carrier `<sector>_transport`) × the battery's `storage_cap_max`. Pairs follow the
     run.py naming rule and run.py's constraint set: the battery-name substring of
     `loc_tech_ev_storage_constraint` is **read from each model's run.py** (currently `_battery` in both models, so
     `heavy_duty_ers_battery` is constrained; with e.g. `ev_battery`, batteries outside it stay free and are listed
     in an info message; not found: `_battery`, warned); a per-pair lookup (`storage_ev__<sector>_<mode>`) lists the nodes where the transport tech
     has a demand-share decision, and the battery needs a finite `storage_cap_max` there, exactly where run.py builds
     the constraint. Added only if the model's run.py has the constraint; a battery outside the naming rule, or
     without a matching demand-share decision, is warned (`storage_ev`), not guessed.
   - **Capacity factors** (`capacity_factor_min/max_constraint`) → `math/capacity_factor.yaml`
     (`capacity_factor_min_limit` / `capacity_factor_max_limit`, the form of the gallery `annual_capacity_factor_*`):
     `sum(flow_out × timestep_weights) >=/<= flow_cap × capacity_factor × sum(timestep_resolution × timestep_weights)`
     over the horizon, on the **primary output carrier** (v0.6 `energy_cap`).
   - **Per-month production** (`carrier_prod_per_month_<sense>_constraint`, built by `run.py` only for the
     `carrier_prod_per_month_<sense>_time_varying` timeseries) → `math/flow_out_per_month.yaml`
     (`flow_out_per_month_<sense>`, parameter `flow_out_per_month_<sense>_relative_per_ts`): per calendar month,
     `sum(flow_out in month) <=/>=/== sum(flow_out) × sum(param × timestep_resolution in month)`, unweighted, on the
     primary output carrier. Months are a new dimension `months` (`month_01` ... `month_12`, calendar months over all
     years, as v0.6 `timesteps.dt.month`) with the lookup `timestep_month` (timesteps × months), generated in
     `data_tables/generated/timestep_month.csv` from the parameters' own CSVs. A value instead of a timeseries, or the
     plain `carrier_prod_per_month_<sense>` alone, is warned (`per_month`): `run.py` builds no constraint from it.
   The other `run.py` constraints (CHP `cb`/`cv`/p2h) are
   still only reported, with their matching math-gallery file; their inputs are carried over (section 3).
2. **`dual_utils.py` is not ported**: duals are a standard v0.7 output through `config.solve.shadow_prices`
   (energy balance and CO2 caps, section 8). `model.to_netcdf` stores them in the `results` group of the file:
   `shadow_price_system_balance` (nodes, carriers, timesteps) and `shadow_price_<CO2 cap constraint>` (costs).
   Read them with `calliope.read_netcdf(path).results` or `xarray.open_dataset(path, group="results")`. A CO2 cap's
   shadow price is <= 0; v0.6 `dual_utils` reported -1 x that.

## 12. Run scripts

1. Every other `*.py` in the legacy model folder (not `run.py`, `create_input.py`, `dual_utils.py`) that defines
   `scenarios_string` is a run script, e.g. `run-cost_base-15-200kW-1h.py`. It is **read, never executed**:
   top-level assignments of strings, numbers, f-strings and `+` are evaluated in order.
2. A v0.7 run script with the same file name is written next to the converted model folder, as in the legacy
   layout (`<folder>/run-*.py` + `<folder>/model/`). It keeps the legacy settings
   and `scenarios_string` verbatim and selects the overrides **by name, as in v0.6**: change e.g.
   `co2_limit = "60"` and run it again. Legacy file paths (`path_to_*`) are replaced by the converted model's.
3. v0.7 `scenario=` refuses overrides that set the same key twice (v0.6 let the later one win; the ERS run script
   selects 20 overrides setting 40 keys twice). The script's `v06_overrides` therefore merges the named overrides in
   order, later ones winning, expanding legacy scenario names (e.g. `uncontrolled`), and passes the result as
   `override_dict`. Nothing is added to the model YAML.
4. The converter checks that every selected override exists (`missing_override` warning otherwise).
5. The script saves inputs, results and shadow prices to `results/<scenario_name>.nc`. The converted math is part
   of the model (conversion_plus, group constraints, demand shares, time-varying limits); the `run.py` constraints
   not translated yet (section 11) are not, and the `dual_utils.py` post-processing is replaced by the standard
   shadow prices (section 11).

## 13. Warnings worth reading

The report groups warnings by code. The ones that usually need a decision:

| code | meaning |
|---|---|
| `missing_lifetime` | **failed check**: techs with investment costs and no `lifetime` (listed with their file); v0.7 cannot annualise their costs and the objective becomes NaN |
| `undefined_tech` / `unknown_tech` | an override or group refers to a tech the model never defines |
| `unreferenced_csv`, `bad_csv`, `csv_nan`, `csv_unsorted`, `missing_column` | timeseries problems |
| `duplicate_carrier` | a tech lists the same carrier twice among its inputs or its outputs; the repeat is ignored (section 6) |
| `override_overlap`, `scenario_merged` | overrides that set the same keys (section 2) |
| `group_redefined` | a group defined differently in several overrides (section 8) |
| `objective_weights` (info) | cost classes excluded from the objective (section 5) |
| `group_family` (info) | per-region groups merged (section 8) |
| `conversion_plus_capacity` (info) | conversion_plus capacity translated as in v0.6 (section 6) |
| `time_varying_limits` (info) | `run.py` time-varying limits translated, with the techs they apply to (section 11) |
| `run_script` (info), `missing_override`, `bad_run_script` | run scripts converted / problems in them (section 12) |
| `custom_param` (info) | a `run.py` input renamed (section 3) |
| `zero_ratio` | a `carrier_ratios` value of 0 (section 6) |
| `unknown_param`, `unknown_key`, `unknown_cost`, `unknown_run_key`, `unknown_model_key`, `unknown_group_constraint` | a v0.6 key with no v0.7 equivalent (parameters are copied unchanged, the rest dropped) |
| `bad_*`, `missing_csv`, `missing_import`, `unknown_node`, `circular_parent` | malformed or missing input; the affected item is skipped |

## 14. Output format

1. YAML mappings are written in block style (one key per line), lists inline unless they hold mappings, with a
   blank line between top-level keys and, in generated math files, between entries.
2. Each converted file starts with a comment naming its legacy source.

## 15. Converter options

Set in `config/config.yaml` (Snakemake) or as arguments of `convert_model` / the CLI:

| option | default | effect |
|---|---|---|
| `entry` (per model) | auto | entry YAML relative to `model/`, when a folder has several |
| `exclude_entries` | `["*_.yaml"]` | file names never used as entry (discarded copies such as `model-2018_.yaml`) |
| `trim_timeseries` | `true` | keep only the legacy `subset_time` in the CSVs (section 7); CLI `--keep-all-timesteps` |
| `copy_csvs` | `true` | copy the legacy CSVs (`false` only for quick tests) |
| `contract_group_families` | `true` | merge per-region group copies into node-indexed groups (section 8); CLI `--no-contract-groups` |

## 16. Structural differences between v0.6 and v0.7 bridged by the converter

Where the same parameter or setting means something different in v0.7, a renamed copy would silently change the
model. The converter bridges each such difference, either by converting the data or by adding custom math, so that a
converted model has the same optimum as the legacy one. Several were found with the consistency checks
(`docs/consistency.md`); any new one gets a row here and a fix in the converter, not only a note.

| difference | v0.6 | v0.7 | what the converter does | found by |
|---|---|---|---|---|
| `om_annual_investment_fraction` | fraction of the **annualised** investment (`costs.py`) | `cost_om_annual_investment_fraction` multiplies the **whole** investment (13.5x more for 60 years at 7.3%) | `math/om_investment_fraction.yaml` redefines `cost_operation_fixed` with `cost_investment_annualised` (section 5) | fixed-design check, ERS year: every transmission link +18.5% |
| time resampling | every timeseries averaged (`resource` summed) | `resample_method` defaults to `first` | custom parameters declare `mean`; other timeseries get it in `math/resample.yaml` (section 7) | ERS year 730 h: EV fleet infeasible |
| objective cost classes | only the classes in `objective_options.cost_class` | unlisted classes weighted 1 | unlisted classes get `objective_cost_weights: 0` (section 5) | ERS: unbounded LP (negative `co2` cost) |
| missing `lifetime` | tolerated | NaN objective, no solver error | `missing_lifetime` warning (section 5) | mini fixture |
| capacity of multi-carrier techs | one `energy_cap`, on the primary output | one `flow_cap` per carrier | limits, costs and groups on the primary carrier (sections 4, 6) | conversion_plus review |
| link distances from x/y | Euclidean distance | lat/lon only | `distance` written on x/y links (section 2) | urban_scale example |
| node names | any string (`region1-1`) | identifiers only | invalid names renamed, also in CSV headers (section 2) | national_scale example |
| keys repeated across overrides | later override wins | refused in one scenario | no redundant keys; clashing scenarios merged (section 2) | ERS overrides |
