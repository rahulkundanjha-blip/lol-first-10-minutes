"""What do the first 10 minutes of a high-rank League of Legends game tell us?

Decision rules, fixed before running:
- Flag a side imbalance for review if the blue-side 95% CI excludes 50% and the
  gap from 50% is at least 1 percentage point.
- Call dragon a higher early priority than herald only if the difference in
  their adjusted effects is significant at alpha = 0.05.
- Treat 10-minute state as a usable live forecast if cross-validated AUC >= 0.75.
"""
import os
import sys

import numpy as np
import pandas as pd
from scipy import stats
import statsmodels.api as sm
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gamestats as gs  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

OUT = os.path.join(HERE, "..", "out")
OBJECTIVES = {"dragon": "Dragon", "herald": "Herald", "tower": "Tower", "first_blood": "First blood", "kills": "Kill"}
GOLD_EDGES = [-np.inf, -4000, -3000, -2000, -1000, 0, 1000, 2000, 3000, 4000, np.inf]


def load():
    d = pd.read_csv(gs.data_path("lol_diamond_10min.csv"))
    assert d.shape == (9879, 40) and d["gameId"].is_unique, "unexpected file"
    assert d.isna().sum().sum() == 0 and set(d["blueWins"]) == {0, 1}
    assert (d["blueTotalGold"] - d["redTotalGold"] == d["blueGoldDiff"]).all()
    return pd.DataFrame({
        "win": d["blueWins"],
        "gold_k": d["blueGoldDiff"] / 1000,
        "dragon": d["blueDragons"] - d["redDragons"],
        "herald": d["blueHeralds"] - d["redHeralds"],
        "tower": d["blueTowersDestroyed"] - d["redTowersDestroyed"],
        "first_blood": d["blueFirstBlood"] * 2 - 1,
        "kills": d["blueKills"] - d["redKills"],
    })


def logit(d, cols):
    return sm.Logit(d["win"], sm.add_constant(d[cols])).fit(disp=0)


def odds(res, cols):
    ci = res.conf_int()
    return {c: {"odds_ratio": float(np.exp(res.params[c])), "ci_low": float(np.exp(ci.loc[c, 0])), "ci_high": float(np.exp(ci.loc[c, 1])),
                "p_value": float(res.pvalues[c]), "p_bonferroni": min(1.0, float(res.pvalues[c]) * len(cols))} for c in cols}


def main():
    gs.style()
    d = load()
    facts = {"checks": {"games": len(d), "duplicate_ids": 0, "null_cells": 0}}
    blue = gs.rate(int(d["win"].sum()), len(d))
    gap = abs(blue["rate"] - .5)
    facts["side"] = {**blue, "gap_from_50": gap, "flag": bool((blue["ci_low"] > .5 or blue["ci_high"] < .5) and gap >= .01)}

    obj = list(OBJECTIVES)
    a, b = logit(d, obj), logit(d, obj + ["gold_k"])
    facts["model_objectives"] = odds(a, obj)
    facts["model_with_gold"] = odds(b, obj + ["gold_k"])
    for name, res in [("objectives_only", a), ("with_gold", b)]:
        t = res.t_test("dragon - herald = 0")
        facts.setdefault("dragon_vs_herald", {})[name] = {"log_odds_difference": float(t.effect[0]), "p_value": float(t.pvalue)}
    facts["dragon_vs_herald"]["dragon_priority"] = bool(facts["dragon_vs_herald"]["objectives_only"]["p_value"] < .05 and facts["dragon_vs_herald"]["objectives_only"]["log_odds_difference"] > 0)

    # Raw win rates for the two neutral objectives.
    facts["raw"] = {"dragon_ahead": gs.rate(int(d[d.dragon > 0].win.sum()), int((d.dragon > 0).sum())),
                    "dragon_behind": gs.rate(int(d[d.dragon < 0].win.sum()), int((d.dragon < 0).sum())),
                    "herald_ahead": gs.rate(int(d[d.herald > 0].win.sum()), int((d.herald > 0).sum())),
                    "herald_behind": gs.rate(int(d[d.herald < 0].win.sum()), int((d.herald < 0).sum()))}

    # Second look: does each side reach the neutral objectives equally?
    side_obj = {}
    for name in ("dragon", "herald"):
        b_, r_ = int((d[name] > 0).sum()), int((d[name] < 0).sum())
        side_obj[name] = {"blue_ahead": b_, "red_ahead": r_, "neither": int((d[name] == 0).sum()), **{k: v for k, v in gs.rate(b_, b_ + r_).items() if k in ("rate", "ci_low", "ci_high")},
                          "p_value": float(stats.binomtest(b_, b_ + r_).pvalue)}
    gold_t = stats.ttest_1samp(d["gold_k"] * 1000, 0)
    side_obj["blue_mean_gold_lead"] = float(d["gold_k"].mean() * 1000)
    side_obj["blue_gold_lead_p"] = float(gold_t.pvalue)
    side_obj["no_neutral_objective_taken"] = gs.rate(int(d[(d.dragon == 0) & (d.herald == 0)].win.sum()), int(((d.dragon == 0) & (d.herald == 0)).sum()))
    facts["side_objectives"] = side_obj

    # Second look: head to head, one team takes dragon and the other takes herald.
    tr = d[((d.dragon == 1) & (d.herald == -1)) | ((d.dragon == -1) & (d.herald == 1))]
    dw = np.where(tr.dragon == 1, tr.win, 1 - tr.win)
    facts["dragon_herald_trade"] = {**gs.rate(int(dw.sum()), len(dw)), "p_value": float(stats.binomtest(int(dw.sum()), len(dw)).pvalue),
                                    "dragon_team_mean_gold_lead": float((tr.gold_k * 1000 * tr.dragon).mean())}

    # Forecast quality, cross-validated.
    cv = StratifiedKFold(5, shuffle=True, random_state=42)
    aucs = {}
    for name, cols in [("objectives", obj), ("gold", ["gold_k"]), ("gold_and_objectives", obj + ["gold_k"])]:
        p = cross_val_predict(make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000)), d[cols], d["win"], cv=cv, method="predict_proba")[:, 1]
        aucs[name] = float(roc_auc_score(d["win"], p))
        if name == "gold_and_objectives":
            facts["forecast_accuracy_at_50pct"] = float(((p >= .5) == d["win"]).mean())
            close = (d["gold_k"].abs() < 1).to_numpy()
            cal = pd.DataFrame({"p": p, "w": d["win"].to_numpy()})
            cal["decile"] = pd.qcut(cal["p"], 10, labels=False)
            by = cal.groupby("decile").agg(p=("p", "mean"), w=("w", "mean"))
            facts["forecast_detail"] = {"close_game_share": float(close.mean()), "accuracy_in_close_games": float(((p >= .5) == d["win"])[close].mean()),
                                        "accuracy_outside_close_games": float(((p >= .5) == d["win"])[~close].mean()),
                                        "largest_calibration_gap": float((by["p"] - by["w"]).abs().max()), "brier": float(((p - d["win"]) ** 2).mean())}
    facts["auc"] = {**aucs, "usable": bool(aucs["gold_and_objectives"] >= .75)}

    # Win rate by gold lead.
    d["bucket"] = pd.cut(d["gold_k"] * 1000, GOLD_EDGES, right=False)
    rows = []
    for b_, g in d.groupby("bucket", observed=True):
        rows.append({"bucket": str(b_), "lo": b_.left, "hi": b_.right, **gs.rate(int(g.win.sum()), len(g))})
    facts["win_by_gold"] = rows

    labels = ["<−4k", "−4 to −3k", "−3 to −2k", "−2 to −1k", "−1k to 0", "0 to 1k", "1 to 2k", "2 to 3k", "3 to 4k", "4k+"]
    fig, ax = plt.subplots(figsize=(10, 4))
    x = np.arange(len(rows))
    ax.bar(x, [100 * r["rate"] for r in rows], color=gs.NEUTRAL, width=.68)
    for i, r in enumerate(rows):
        ax.text(i, 100 * r["rate"] + 1.5, f"{100*r['rate']:.0f}%", ha="center", fontsize=9.5)
    ax.axhline(50, color=gs.INK2, lw=1)
    ax.set_xticks(x, labels); ax.tick_params(axis="x", colors=gs.INK2, length=0, labelsize=9.5)
    ax.set_ylim(0, 105); ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:.0f}%"))
    ax.set_xlabel("blue side's gold lead at 10 minutes"); ax.set_title("Blue-side win rate by gold lead at 10 minutes")
    gs.save_fig(fig, OUT, "01_win_by_gold.png")

    fig, ax = plt.subplots(figsize=(10, 4))
    y = np.arange(len(obj))[::-1]
    for model, col, off, name in [(facts["model_objectives"], gs.SERIES[0], .14, "objectives only"), (facts["model_with_gold"], gs.SERIES[1], -.14, "also holding gold fixed")]:
        for yy, c in zip(y, obj):
            r = model[c]
            ax.plot([r["ci_low"], r["ci_high"]], [yy + off] * 2, color=col, zorder=2)
            ax.scatter(r["odds_ratio"], yy + off, s=48, color=col, edgecolor=gs.SURFACE, linewidth=2, zorder=3, label=name if c == obj[0] else None)
    ax.axvline(1, color=gs.INK2, lw=1)
    ax.set_yticks(y, [OBJECTIVES[c] for c in obj]); ax.tick_params(axis="y", colors=gs.INK2)
    ax.set_xscale("log"); ax.set_xticks([.8, 1, 1.5, 2, 3]); ax.set_xticklabels(["0.8", "1", "1.5", "2", "3"]); ax.minorticks_off()
    ax.grid(axis="y", visible=False); ax.grid(axis="x", visible=True)
    ax.set_xlabel("odds ratio for winning, per unit of lead (95% CI, log scale)")
    ax.set_title("What each early objective is worth")
    ax.legend(loc="lower right")
    gs.save_fig(fig, OUT, "02_objective_odds.png")
    gs.save_results(facts, OUT)
    return facts


if __name__ == "__main__":
    import json
    print(json.dumps(main(), indent=1, default=float))
