# Reviews

How to use: add a new item under **Open** (or a `Comment:` line under an existing one). When you say so,
Claude tackles every item in one go and leaves here only what is still open.

## Open

## Future developments

### F1. Shadow prices in the v0.6 vs v0.7 comparison

- Dropped from `compare` for now (owner's choice).
- To add back: energy balance per (node, carrier, timestep) and CO2 cap prices.
  - v0.6 from the legacy `dual_utils` outputs (`process_system_balance_duals`, `process_co2_duals`; v0.6 `.nc` files
    have no duals).
  - v0.7 from `shadow_price_system_balance` and `shadow_price_<CO2 cap constraint>` (results group).
- Needs a solver returning duals on both sides (Calliope v0.7 reads none from CBC; e.g. Gurobi or HiGHS).
- v0.6 `dual_utils` reports the CO2 cap price with a flipped sign.
