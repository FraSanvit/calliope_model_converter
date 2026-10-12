"""Static v0.6 -> v0.7 renaming knowledge.

Sources: Calliope `docs/migrating.md` (v0.7.0), the v0.6.10 `defaults.yaml` and
the v0.7.0 `math/base.yaml`. Base-tech dependent renames (`energy_eff`,
`resource`, `om_con`, ...) are handled in `converter.techs`.
"""

from __future__ import annotations

# Plain 1:1 renames of v0.6 tech `constraints` (base-tech independent).
PARAM_RENAMES: dict[str, str] = {
    "energy_cap_max": "flow_cap_max",
    "energy_cap_min": "flow_cap_min",
    "energy_cap_max_systemwide": "flow_cap_max_systemwide",
    "energy_cap_min_systemwide": "flow_cap_min_systemwide",
    "energy_cap_min_use": "flow_out_min_relative",
    "energy_cap_per_unit": "flow_cap_per_unit",
    "energy_cap_per_storage_cap_max": "flow_cap_per_storage_cap_max",
    "energy_cap_per_storage_cap_min": "flow_cap_per_storage_cap_min",
    "charge_rate": "flow_cap_per_storage_cap_max",
    "energy_eff_per_distance": "flow_out_eff_per_distance",
    "energy_ramping": "flow_ramping",
    "parasitic_eff": "flow_out_parasitic_eff",
    "resource_eff": "source_eff",
    "resource_cap_max": "source_cap_max",
    "resource_cap_min": "source_cap_min",
    "resource_cap_equals_energy_cap": "source_cap_equals_flow_cap",
    "resource_area_max": "area_use_max",
    "resource_area_min": "area_use_min",
    "resource_area_per_energy_cap": "area_use_per_flow_cap",
    "storage_cap_max": "storage_cap_max",
    "storage_cap_min": "storage_cap_min",
    "storage_cap_per_unit": "storage_cap_per_unit",
    "storage_initial": "storage_initial",
    "storage_loss": "storage_loss",
    "storage_discharge_depth": "storage_discharge_depth",
    "lifetime": "lifetime",
    "one_way": "one_way",
    "export_cap": "export_max",
    "force_asynchronous_prod_con": "force_async_flow",
    "units_max": "purchased_units_max",
    "units_min": "purchased_units_min",
    "units_max_systemwide": "purchased_units_max_systemwide",
    "distance": "distance",
}

# v0.6 `*_equals` constraints become `*_min` == `*_max` in v0.7.
EQUALS_RENAMES: dict[str, tuple[str, str]] = {
    "energy_cap_equals": ("flow_cap_min", "flow_cap_max"),
    "energy_cap_equals_systemwide": ("flow_cap_min_systemwide", "flow_cap_max_systemwide"),
    "energy_cap_per_storage_cap_equals": ("flow_cap_per_storage_cap_min", "flow_cap_per_storage_cap_max"),
    "storage_cap_equals": ("storage_cap_min", "storage_cap_max"),
    "resource_cap_equals": ("source_cap_min", "source_cap_max"),
    "resource_area_equals": ("area_use_min", "area_use_max"),
    "units_equals": ("purchased_units_min", "purchased_units_max"),
    "units_equals_systemwide": ("purchased_units_min_systemwide", "purchased_units_max_systemwide"),
}

# v0.7 params that live on the capacity carrier of multi-carrier (conversion) techs.
CAP_CARRIER_PARAMS = {
    "flow_cap_max",
    "flow_cap_min",
    "flow_cap_max_systemwide",
    "flow_cap_min_systemwide",
    "flow_out_min_relative",
    "flow_cap_per_unit",
}

# v0.6 keys that no longer exist and carry no information in v0.7.
DROPPED_PARAMS = {"energy_con", "energy_prod", "force_resource"}

# v0.6 keys that have no v0.7 equivalent and need user attention.
UNSUPPORTED_PARAMS = {
    "energy_cap_scale": "no v0.7 equivalent; pre-scale the capacity values instead",
    "resource_scale": "no v0.7 equivalent; pre-scale the resource values instead",
    "resource_min_use": "v0.7 `source_use_min` is absolute, not a fraction of available resource",
    "storage_time_max": "removed in v0.7",
}

# v0.6 cost keys -> v0.7 cost parameters. `om_con` is base-tech dependent.
COST_RENAMES: dict[str, str] = {
    "energy_cap": "cost_flow_cap",
    "energy_cap_per_distance": "cost_flow_cap_per_distance",
    "storage_cap": "cost_storage_cap",
    "resource_cap": "cost_source_cap",
    "resource_area": "cost_area_use",
    "om_annual": "cost_om_annual",
    "om_annual_investment_fraction": "cost_om_annual_investment_fraction",
    "om_prod": "cost_flow_out",
    "export": "cost_export",
    "purchase": "cost_purchase",
    "purchase_per_distance": "cost_purchase_per_distance",
    "interest_rate": "cost_interest_rate",
    "depreciation_rate": "cost_depreciation_rate",
}

# v0.7 cost params that are multiplied by a carrier-indexed variable
# (flow_cap / flow_out / flow_in / flow_export) and must be carrier indexed for
# multi-carrier techs, otherwise they apply to every carrier of the tech.
CARRIER_COSTS = {
    "cost_flow_cap",
    "cost_flow_cap_per_distance",
    "cost_flow_out",
    "cost_flow_in",
    "cost_export",
    "cost_om_annual",
}

RESOURCE_UNITS = {"energy": "absolute", "energy_per_cap": "per_cap", "energy_per_area": "per_area"}

# Legacy custom parameters consumed by run.py. Renamed to the math-gallery
# names where a gallery equivalent exists, otherwise to v0.7-style names.
# value: (v0.7 name, gallery file or None)
CUSTOM_PARAMS: dict[str, tuple[str, str | None]] = {
    "energy_cap_max_time_varying": ("flow_cap_max_relative_per_ts", "max_time_varying.yaml"),
    "energy_cap_equals_time_varying": ("flow_cap_equals_relative_per_ts", None),
    "energy_cap_max_time_varying_con": ("flow_in_max_relative_per_ts", None),
    "storage_charge_depth": ("storage_charge_depth", None),
    "cb": ("power_to_heat_ratio", "chp_htp.yaml"),
    "cv": ("power_loss_factor", "chp_htp.yaml"),
    "energy_cap_ratio": ("flow_cap_ratio", None),
    "capacity_factor_min": ("capacity_factor_min", "uptime_downtime_limits.yaml"),
    "capacity_factor_max": ("capacity_factor_max", "uptime_downtime_limits.yaml"),
    "carrier_prod_per_month_min": ("flow_out_per_month_min", None),
    "carrier_prod_per_month_max": ("flow_out_per_month_max", None),
    "carrier_prod_per_month_equals": ("flow_out_per_month_equals", None),
    "carrier_prod_per_month_min_time_varying": ("flow_out_per_month_min_relative_per_ts", None),
    "carrier_prod_per_month_max_time_varying": ("flow_out_per_month_max_relative_per_ts", None),
    "carrier_prod_per_month_equals_time_varying": ("flow_out_per_month_equals_relative_per_ts", None),
}

GALLERY_URL = "https://calliope.readthedocs.io/en/latest/examples/math_gallery/"

# run.py custom constraint -> (math gallery file or None, note for the report)
RUNPY_GALLERY: dict[str, tuple[str | None, str]] = {
    "carrier_production_max_time_varying_constraint": (
        "max_time_varying.yaml",
        "TRANSLATED: math/time_varying_limits.yaml `max_time_varying_flow_out` (`flow_cap_max_relative_per_ts`).",
    ),
    "carrier_production_equals_time_varying_constraint": (
        "max_time_varying.yaml",
        "TRANSLATED: math/time_varying_limits.yaml `equals_time_varying_flow_out` (`flow_cap_equals_relative_per_ts`).",
    ),
    "carrier_consumption_max_time_varying_constraint": (
        "max_time_varying.yaml",
        "TRANSLATED: math/time_varying_limits.yaml `max_time_varying_flow_in` (`flow_in_max_relative_per_ts`).",
    ),
    "chp_extraction_cb_constraint": (
        "chp_htp.yaml",
        "Gallery `chp_backpressure_line_min`; `cb` -> `power_to_heat_ratio`. "
        "Legacy applies it to CHPs with `cb` and no `energy_cap_ratio`; gallery selects via `turbine_type`.",
    ),
    "chp_extraction_cv_constraint": (
        "chp_htp.yaml",
        "Gallery `chp_extraction_line`; `cv` -> `power_loss_factor`. Legacy bounds by `energy_cap`, "
        "the gallery by fuel input times electrical efficiency.",
    ),
    "chp_extraction_p2h_constraint": (
        None,
        "No gallery equivalent (power-to-heat tail); uses `power_to_heat_ratio` and `flow_cap_ratio`.",
    ),
    "capacity_factor_min_constraint": (
        "uptime_downtime_limits.yaml",
        "TRANSLATED: math/capacity_factor.yaml `capacity_factor_min_limit` (gallery `annual_capacity_factor_min`), "
        "on the primary output carrier.",
    ),
    "capacity_factor_max_constraint": (
        "uptime_downtime_limits.yaml",
        "TRANSLATED: math/capacity_factor.yaml `capacity_factor_max_limit` (gallery `annual_capacity_factor_max`), "
        "on the primary output carrier.",
    ),
    "carrier_prod_per_month_min_constraint": (
        None,
        "TRANSLATED: math/flow_out_per_month.yaml `flow_out_per_month_min`: output of the primary carrier in each calendar month vs the horizon's output x the month's sum of the timeseries (lookup `timestep_month`).",
    ),
    "carrier_prod_per_month_max_constraint": (
        None,
        "TRANSLATED: math/flow_out_per_month.yaml `flow_out_per_month_max`: output of the primary carrier in each calendar month vs the horizon's output x the month's sum of the timeseries (lookup `timestep_month`).",
    ),
    "carrier_prod_per_month_equals_constraint": (
        None,
        "TRANSLATED: math/flow_out_per_month.yaml `flow_out_per_month_equals`: output of the primary carrier in each calendar month vs the horizon's output x the month's sum of the timeseries (lookup `timestep_month`).",
    ),
    "fuel_distribution_constraint": (
        "fuel_dist.yaml",
        "TRANSLATED: math/fuel_distribution.yaml `fuel_distribution_balance` (gallery `restrict_total_imports_and_"
        "exports` form): per carrier, imports == exports over all nodes and timesteps (as run.py).",
    ),
    "storage_ev_constraint": (
        None,
        "TRANSLATED: math/storage_ev.yaml `storage_ev_<sector>_<mode>`: storage_cap of `<sector>_<mode>_battery` == "
        "demand_share_per_timestep_decision of `<sector>_transport_<mode>` x storage_cap_max (run.py naming rule; battery-name filter read from run.py's set).",
    ),
}
