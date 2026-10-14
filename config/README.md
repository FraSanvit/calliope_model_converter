We recommend consulting the following before using this module:
- `config/config.yaml`: a generic example configuration of this module.
- `workflow/internal/config.schema.yaml`: a schematic overview of all the configuration options of this module.
- `INTERFACE.yaml`: lists module input and output files, and their default locations.
- `tests/integration/Snakefile`: an example of how to call this module from another workflow.

## Options

| key | meaning |
|---|---|
| `models` | Legacy models to convert, keyed by folder name in `resources/user/`. Optionally set `entry` (YAML relative to `model/`) when a folder has several model entry files. |
| `exclude_entries` | File-name patterns never used as entry file (default `["*_.yaml"]`, i.e. discarded copies such as `model-2018_.yaml`). |
| `trim_timeseries` | Keep only the timesteps inside the legacy `subset_time` (default `true`; v0.7 loads whole tables before subsetting). |
| `copy_csvs` | Copy all legacy CSVs (default `true`; `false` only for quick tests). |
| `contract_group_families` | Write per-region copies of a legacy group (`biofuel_1..4`, `ALB_1_diesel`/`AUT_2_diesel`: one location each, same techs and constraints) as one group with values indexed on `nodes` (default `true`; `false`: one constraint per legacy group). |

This data module is part of the [Modelblocks](https://www.modelblocks.org/) project.
Please consult the [Modelblocks documentation](https://modelblocks.readthedocs.io/) for more details.
