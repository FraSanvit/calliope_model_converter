"""Trimmed legacy run script used by the tests (static analysis only, never executed)."""


def update_constraint_sets(model):
    if any("_battery" in tech for tech in model._model_data.techs.values):
        model._model_data.coords["loc_tech_ev_storage_constraint"] = [
            loc_techs_store
            for loc_techs_store in model._model_data.loc_techs_store.values
            if (model.inputs.storage_cap_max.loc[{"loc_techs_store": loc_techs_store}].notnull())
            and ("ev_battery" in loc_techs_store.split("::")[1])
        ]
    return model


def add_eurocalliope_constraints(model):
    if "energy_cap_max_time_varying_con" in model._model_data.data_vars:
        add_consumption_max_time_varying_constraint(model)
    if "cb" in model._model_data.data_vars:
        add_chp_cb_constraint(model)
    if any("_battery" in tech for tech in model._model_data.techs.values):
        add_storage_ev_constraint(model)


def add_consumption_max_time_varying_constraint(model):
    def _rule(backend_model, loc_tech, timestep):
        return backend_model.energy_cap_max_time_varying_con[loc_tech, timestep]

    model.backend.add_constraint(
        "carrier_consumption_max_time_varying_constraint",
        ["loc_tech_carrier_consumption_max_time_varying_constraint", "timesteps"],
        _rule,
    )


def add_chp_cb_constraint(model):
    def _rule(backend_model, loc_tech, timestep):
        return get_param(backend_model, "cb", (loc_tech))  # noqa: F821

    model.backend.add_constraint("chp_extraction_cb_constraint", ["loc_techs", "timesteps"], _rule)


def add_storage_ev_constraint(model):
    def _rule(backend_model, loc_tech):
        return backend_model.demand_share_per_timestep_decision[loc_tech]

    model.backend.add_constraint("storage_ev_constraint", ["loc_tech_ev_storage_constraint"], _rule)
