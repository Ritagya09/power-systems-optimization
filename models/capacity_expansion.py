"""
Capacity Expansion Planning Model
----------------------------------
This model answers: what new power plants should we BUILD to meet
future electricity demand at the lowest total cost (capital + operating)?

It uses Mixed-Integer Linear Programming (MILP) via PyPSA + HiGHS solver.

Key difference from Unit Commitment:
- Unit Commitment  → which existing plants to RUN each hour (operational)
- Capacity Expansion → which NEW plants to BUILD over years (investment)
"""

import pypsa
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

# ── 1. LOAD DATA ──────────────────────────────────────────────────────────────

generators = pd.read_csv("data/generators.csv", index_col="name")
demand     = pd.read_csv("data/demand.csv",     index_col="hour")
cf         = pd.read_csv("data/capacity_factors.csv", index_col="hour")

print("✅ Data loaded successfully")

# ── 2. INVESTMENT COST PARAMETERS ─────────────────────────────────────────────
# Capital costs are annualized using the formula:
#   Annualized CAPEX = CAPEX * [r(1+r)^n / ((1+r)^n - 1)]
# where r = discount rate, n = asset lifetime in years
#
# This converts a one-time build cost into a yearly cost,
# so we can compare it fairly against annual operating costs.

discount_rate = 0.07   # 7% — typical for energy projects
lifetime      = 25     # years — typical plant lifetime

def annualize(capex, r=discount_rate, n=lifetime):
    """Convert one-time capital cost to annual cost."""
    crf = r * (1 + r)**n / ((1 + r)**n - 1)   # Capital Recovery Factor
    return capex * crf

# Technology options available to build ($/MW installed)
# These are realistic 2024 numbers
investment_options = {
    "New_Solar":    {"capex": 800_000,   "marginal_cost": 0,  "type": "solar"},
    "New_Wind":     {"capex": 1_200_000, "marginal_cost": 0,  "type": "wind"},
    "New_Gas_CCGT": {"capex": 900_000,   "marginal_cost": 55, "type": "gas"},
    "New_Battery":  {"capex": 600_000,   "marginal_cost": 0,  "type": "storage"},
}

print("\n📊 Annualized Capital Costs ($/MW/year):")
for name, params in investment_options.items():
    ann = annualize(params["capex"])
    print(f"   {name:<15} ${ann:>10,.0f}/MW/year  (total CAPEX: ${params['capex']:>10,})")

# ── 3. FUTURE DEMAND SCENARIO ─────────────────────────────────────────────────
# We assume demand grows 30% over the planning horizon
# The model must find the cheapest way to meet this higher demand

demand_growth_factor = 1.30
future_demand = demand["demand_mw"] * demand_growth_factor

print(f"\n📈 Demand growth factor: {demand_growth_factor}x")
print(f"   Peak demand today : {demand['demand_mw'].max():.0f} MW")
print(f"   Peak demand future: {future_demand.max():.0f} MW")

# ── 4. BUILD THE NETWORK ──────────────────────────────────────────────────────

network = pypsa.Network()
hours = pd.RangeIndex(24, name="hour")
network.set_snapshots(hours)
network.add("Bus", "grid")

# Add future demand load
network.add(
    "Load", "future_demand",
    bus="grid",
    p_set=future_demand.values
)

# ── 5. ADD EXISTING GENERATORS (already built, sunk cost) ─────────────────────
# Existing plants have zero capital cost — they're already paid for.
# We only consider their operating costs.

print("\n🏭 Adding existing generators...")
for name, row in generators.iterrows():
    is_renewable = row["type"] in ["solar", "wind"]

    if is_renewable:
        if row["type"] == "solar":
            p_max_pu = (cf["solar"]).values
        else:
            p_max_pu = (cf["wind"]).values

        network.add(
            "Generator", f"Existing_{name}",
            bus="grid",
            p_nom=row["capacity_mw"],
            p_nom_extendable=False,   # cannot expand existing
            marginal_cost=0.0,
            p_max_pu=p_max_pu,
        )
    else:
        network.add(
            "Generator", f"Existing_{name}",
            bus="grid",
            p_nom=row["capacity_mw"],
            p_nom_extendable=False,
            marginal_cost=row["marginal_cost"],
        )

# ── 6. ADD CANDIDATE GENERATORS (can be built, optimizer decides how much) ────
# p_nom_extendable=True means the optimizer DECIDES the capacity to build
# p_nom_min=0 means we don't have to build any
# p_nom_max sets a realistic upper limit

print("🔧 Adding investment candidates...")
for name, params in investment_options.items():
    ann_cost = annualize(params["capex"])

    if params["type"] == "solar":
        network.add(
            "Generator", name,
            bus="grid",
            p_nom_extendable=True,
            p_nom_min=0,
            p_nom_max=1000,           # max 1000 MW can be built
            capital_cost=ann_cost,
            marginal_cost=0.0,
            p_max_pu=cf["solar"].values,
        )
    elif params["type"] == "wind":
        network.add(
            "Generator", name,
            bus="grid",
            p_nom_extendable=True,
            p_nom_min=0,
            p_nom_max=1000,
            capital_cost=ann_cost,
            marginal_cost=0.0,
            p_max_pu=cf["wind"].values,
        )
    elif params["type"] == "gas":
        network.add(
            "Generator", name,
            bus="grid",
            p_nom_extendable=True,
            p_nom_min=0,
            p_nom_max=1000,
            capital_cost=ann_cost,
            marginal_cost=params["marginal_cost"],
        )
    elif params["type"] == "storage":
        # Battery modeled as a StorageUnit
        network.add(
            "StorageUnit", name,
            bus="grid",
            p_nom_extendable=True,
            p_nom_min=0,
            p_nom_max=500,
            capital_cost=ann_cost,
            marginal_cost=0.0,
            max_hours=4,              # 4-hour battery
            efficiency_store=0.95,
            efficiency_dispatch=0.95,
            cyclic_state_of_charge=True,
        )

print("✅ Network built successfully")

# ── 7. SOLVE THE CAPACITY EXPANSION PROBLEM ───────────────────────────────────
# Objective: minimize total annual cost = capital cost + operating cost
# Subject to:
#   - supply meets demand every hour
#   - capacity limits respected
#   - storage energy balance

print("\n🔄 Solving Capacity Expansion (LP)...")
print("   This may take a few seconds...\n")

network.optimize(solver_name="highs")

print("✅ Optimization complete!")

# ── 8. EXTRACT RESULTS ────────────────────────────────────────────────────────

print("\n" + "="*55)
print("📊 OPTIMAL INVESTMENT DECISIONS")
print("="*55)

# New capacity built
new_generators = network.generators[network.generators.p_nom_extendable]
new_storage    = network.storage_units[network.storage_units.p_nom_extendable]

investment_results = {}

for name in new_generators.index:
    capacity = network.generators.loc[name, "p_nom_opt"]
    ann_cost = network.generators.loc[name, "capital_cost"]
    if capacity > 0.01:
        investment_results[name] = {
            "capacity_mw": capacity,
            "annual_cost": capacity * ann_cost,
            "type": "generator"
        }
        print(f"   {name:<15} → Build {capacity:>7.1f} MW  (annual cost: ${capacity * ann_cost:>10,.0f})")

for name in new_storage.index:
    capacity = network.storage_units.loc[name, "p_nom_opt"]
    ann_cost = network.storage_units.loc[name, "capital_cost"]
    if capacity > 0.01:
        investment_results[name] = {
            "capacity_mw": capacity,
            "annual_cost": capacity * ann_cost,
            "type": "storage"
        }
        print(f"   {name:<15} → Build {capacity:>7.1f} MW  (annual cost: ${capacity * ann_cost:>10,.0f})")

total_annual = sum(v["annual_cost"] for v in investment_results.values())
print(f"\n💰 Total Annual System Cost: ${network.objective:,.0f}")
print(f"💰 Total New Investment Cost: ${total_annual:,.0f}/year")

# Save investment results
inv_df = pd.DataFrame(investment_results).T
inv_df.to_csv("results/expansion_investments.csv")

# Save dispatch
dispatch = network.generators_t.p
dispatch.to_csv("results/expansion_dispatch.csv")
print("\n✅ Results saved to results/")

# ── 9. VISUALIZE RESULTS ──────────────────────────────────────────────────────

fig, axes = plt.subplots(1, 2, figsize=(14, 6))

colors = {
    "New_Solar":           "#f1c40f",
    "New_Wind":            "#2ecc71",
    "New_Gas_CCGT":        "#e07b39",
    "New_Battery":         "#9b59b6",
    "Existing_Coal_Plant": "#4a4a4a",
    "Existing_Gas_Plant_1":"#e07b39",
    "Existing_Gas_Plant_2":"#e0a039",
    "Existing_Oil_Peaker": "#c0392b",
    "Existing_Solar_Farm": "#f9ca24",
    "Existing_Wind_Farm":  "#badc58",
}

# ── Plot 1: Optimal capacity mix (MW) ──
ax1 = axes[0]
all_gens = {}

for name in network.generators.index:
    cap = network.generators.loc[name, "p_nom_opt"] if network.generators.loc[name, "p_nom_extendable"] else network.generators.loc[name, "p_nom"]
    if cap > 0.1:
        all_gens[name] = cap

for name in network.storage_units.index:
    cap = network.storage_units.loc[name, "p_nom_opt"] if network.storage_units.loc[name, "p_nom_extendable"] else network.storage_units.loc[name, "p_nom"]
    if cap > 0.1:
        all_gens[name] = cap

cap_series = pd.Series(all_gens).sort_values(ascending=True)
bar_colors = [colors.get(n, "#95a5a6") for n in cap_series.index]
cap_series.plot(kind="barh", ax=ax1, color=bar_colors, alpha=0.85)
ax1.set_title("Optimal Capacity Mix", fontsize=13, fontweight="bold")
ax1.set_xlabel("Capacity (MW)")
ax1.grid(axis="x", alpha=0.3)
for i, (val, name) in enumerate(zip(cap_series.values, cap_series.index)):
    ax1.text(val + 2, i, f"{val:.0f} MW", va="center", fontsize=8)

# ── Plot 2: Dispatch over 24 hours ──
ax2 = axes[1]
bottom = np.zeros(24)
dispatch_cols = [c for c in dispatch.columns if dispatch[c].sum() > 0]

for gen in dispatch_cols:
    values = dispatch[gen].values
    ax2.bar(
        range(24), values, bottom=bottom,
        label=gen, color=colors.get(gen, "#95a5a6"),
        alpha=0.85, width=0.8
    )
    bottom += values

ax2.plot(
    range(24), future_demand.values,
    color="black", linewidth=2,
    linestyle="--", label="Future Demand", zorder=5
)
ax2.set_title("Optimal Dispatch — Future Demand", fontsize=13, fontweight="bold")
ax2.set_xlabel("Hour of Day")
ax2.set_ylabel("Power (MW)")
ax2.set_xticks(range(24))
ax2.legend(loc="upper left", fontsize=7)
ax2.grid(axis="y", alpha=0.3)

plt.suptitle("Capacity Expansion Planning Results", fontsize=15, fontweight="bold", y=1.02)
plt.tight_layout()
plt.savefig("results/expansion_plan_chart.png", dpi=150, bbox_inches="tight")
plt.show()
print("✅ Chart saved to results/expansion_plan_chart.png")
