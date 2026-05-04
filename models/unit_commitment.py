"""
Unit Commitment Model
---------------------
This model answers: which power plants should we turn on/off each hour
to meet electricity demand at the lowest possible cost?

It uses Mixed-Integer Linear Programming (MILP) via PyPSA + HiGHS solver.
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
print(f"   Generators : {len(generators)}")
print(f"   Time steps : {len(demand)} hours")

# ── 2. BUILD THE NETWORK ──────────────────────────────────────────────────────
# A PyPSA "network" is the container for everything:
# buses (nodes), generators, loads, and the time series.

network = pypsa.Network()

# Time index: 24 hours (0 to 23)
hours = pd.RangeIndex(24, name="hour")
network.set_snapshots(hours)

# Add a single bus (node) — think of it as one big grid
network.add("Bus", "grid")

# ── 3. ADD LOAD (DEMAND) ──────────────────────────────────────────────────────
# This is the electricity demand the grid must meet every hour.

network.add(
    "Load",
    "demand",
    bus="grid",
    p_set=demand["demand_mw"].values  # MW demand for each of 24 hours
)

print("✅ Demand added to network")

# ── 4. ADD GENERATORS ─────────────────────────────────────────────────────────
# Each generator has:
#   p_nom        = max capacity (MW)
#   marginal_cost= cost per MWh generated ($/MWh)
#   committable  = True means MILP (on/off decisions)
#   p_min_pu     = minimum output when ON (as fraction of capacity)

for name, row in generators.iterrows():
    is_renewable = row["type"] in ["solar", "wind"]

    if is_renewable:
        # Renewables: output limited by capacity factor, no committable needed
        if row["type"] == "solar":
            p_max_series = (cf["solar"] * row["capacity_mw"]).values
        else:
            p_max_series = (cf["wind"]  * row["capacity_mw"]).values

        network.add(
            "Generator",
            name,
            bus="grid",
            p_nom=row["capacity_mw"],
            marginal_cost=0.0,
            p_max_pu=p_max_series / row["capacity_mw"],  # fraction per hour
        )
    else:
        # Thermal plants: committable (on/off binary decisions)
        network.add(
            "Generator",
            name,
            bus="grid",
            p_nom=row["capacity_mw"],
            marginal_cost=row["marginal_cost"],
            committable=True,
            p_min_pu=0.3,       # must run at ≥30% capacity when ON
            up_time_before=0,
            start_up_cost=row["startup_cost"],
        )

print("✅ All generators added to network")

# ── 5. SOLVE THE UNIT COMMITMENT PROBLEM ──────────────────────────────────────
# PyPSA builds the MILP and sends it to HiGHS to solve.
# Objective: minimize total cost (fuel + startup costs) subject to:
#   - supply must meet demand every hour
#   - generators respect their capacity limits
#   - thermal plants respect min up/down times

print("\n🔄 Solving Unit Commitment (MILP)...")
print("   This may take a few seconds...\n")

network.optimize(solver_name="highs")

print("\n✅ Optimization complete!")

# ── 6. EXTRACT RESULTS ────────────────────────────────────────────────────────

# Generation dispatch — how much each plant produces each hour
dispatch = network.generators_t.p
print("\n📊 Dispatch Results (MW per hour):")
print(dispatch.round(1).to_string())

# Total system cost
total_cost = network.objective
print(f"\n💰 Total System Cost: ${total_cost:,.0f}")

# Save dispatch to CSV
dispatch.to_csv("results/uc_dispatch.csv")
print("\n✅ Results saved to results/uc_dispatch.csv")

# ── 7. VISUALIZE RESULTS ──────────────────────────────────────────────────────

fig, axes = plt.subplots(2, 1, figsize=(12, 8))

# ── Plot 1: Stacked dispatch chart ──
colors = {
    "Coal_Plant":  "#4a4a4a",
    "Gas_Plant_1": "#e07b39",
    "Gas_Plant_2": "#e0a039",
    "Oil_Peaker":  "#c0392b",
    "Solar_Farm":  "#f1c40f",
    "Wind_Farm":   "#2ecc71",
}

ax1 = axes[0]
bottom = np.zeros(24)
for gen in dispatch.columns:
    values = dispatch[gen].values
    ax1.bar(
        range(24), values, bottom=bottom,
        label=gen, color=colors.get(gen, "#999"),
        alpha=0.85, width=0.8
    )
    bottom += values

# Overlay demand line
ax1.plot(
    range(24), demand["demand_mw"].values,
    color="black", linewidth=2,
    linestyle="--", label="Demand", zorder=5
)

ax1.set_title("Unit Commitment — Hourly Dispatch Schedule", fontsize=14, fontweight="bold")
ax1.set_xlabel("Hour of Day")
ax1.set_ylabel("Power (MW)")
ax1.set_xticks(range(24))
ax1.legend(loc="upper left", fontsize=8)
ax1.grid(axis="y", alpha=0.3)

# ── Plot 2: Cost breakdown by generator ──
ax2 = axes[1]
gen_costs = {}
for gen in dispatch.columns:
    if gen in network.generators.index:
        mc = network.generators.loc[gen, "marginal_cost"]
        gen_costs[gen] = (dispatch[gen] * mc).sum()

cost_series = pd.Series(gen_costs).sort_values(ascending=False)
bars = ax2.bar(
    cost_series.index, cost_series.values,
    color=[colors.get(g, "#999") for g in cost_series.index],
    alpha=0.85
)
ax2.set_title("Total Operating Cost by Generator", fontsize=14, fontweight="bold")
ax2.set_xlabel("Generator")
ax2.set_ylabel("Cost ($)")
ax2.grid(axis="y", alpha=0.3)

for bar, val in zip(bars, cost_series.values):
    ax2.text(
        bar.get_x() + bar.get_width() / 2,
        bar.get_height() + 100,
        f"${val:,.0f}",
        ha="center", va="bottom", fontsize=8
    )

plt.tight_layout()
plt.savefig("results/uc_dispatch_chart.png", dpi=150, bbox_inches="tight")
plt.show()
print("✅ Chart saved to results/uc_dispatch_chart.png")
