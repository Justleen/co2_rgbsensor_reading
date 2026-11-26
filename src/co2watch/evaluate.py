"""Score the detectors against simulated anomalies.

Event-level metrics, because that is what matters for alerts:

* an injected anomaly counts as **detected** when at least one alert overlaps
  its labelled window, widened by ``tolerance_before`` / ``tolerance_after``
  (daily summaries can only fire once the day's window has passed);
* an alert that overlaps no labelled window is a **false alarm**;
* false alarms per week are measured on the anomaly runs (outside the labelled
  windows) and on clean runs with no anomalies at all.

These numbers describe the simulator, not the tank. They show whether the
detectors behave as designed; real performance needs real, labelled data (M6).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .detect import DetectConfig, detect
from .features import prepare
from .simulator import default_scenario, simulate


@dataclass
class EvalResult:
    per_kind: pd.DataFrame      # kind, injected, detected, recall, first_alert_delay_h
    false_alarms: pd.DataFrame  # run, weeks, false_alarms, per_week
    alerts: pd.DataFrame        # every alert with run id and matched label


def match(alerts: pd.DataFrame, labels: pd.DataFrame, before="1h", after="8h") -> pd.DataFrame:
    a = alerts.copy()
    a["matched"] = None
    lab = labels[labels["category"] != "calibration_event"]
    for i, al in a.iterrows():
        for _, lb in lab.iterrows():
            if al["start"] <= lb["end"] + pd.Timedelta(after) and \
                    al["end"] >= lb["start"] - pd.Timedelta(before):
                a.at[i, "matched"] = lb["kind"]
                break
    return a


def evaluate(seeds=(1, 2, 3), days: int = 42, cfg: DetectConfig | None = None,
             clean: bool = True) -> EvalResult:
    cfg = cfg or DetectConfig()
    kinds, fa_rows, all_alerts = [], [], []
    for seed in seeds:
        for mode in (["anomalies", "clean"] if clean else ["anomalies"]):
            res = simulate(days=days, seed=seed,
                           anomalies=default_scenario(days) if mode == "anomalies" else [])
            events = list(res.labels.loc[res.labels["category"] == "calibration_event", "start"])
            det = detect(prepare(res.readings), cfg, events)
            m = match(det.alerts, res.labels)
            m["run"] = f"{mode}-{seed}"
            all_alerts.append(m)
            lab = res.labels[res.labels["category"] != "calibration_event"]
            for _, lb in lab.iterrows():
                hits = m[m["matched"] == lb["kind"]]
                # time from the anomaly's start until an alert could be raised live
                delay = ((hits["known_at"].min() - lb["start"]).total_seconds() / 3600
                         if len(hits) else None)
                kinds.append({"kind": lb["kind"], "category": lb["category"],
                              "detected": len(hits) > 0, "delay_h": delay,
                              "detector": hits["kind"].mode().iloc[0] if len(hits) else None})
            fa_rows.append({"run": f"{mode}-{seed}", "weeks": days / 7,
                            "false_alarms": int(m["matched"].isna().sum())})
    k = pd.DataFrame(kinds)
    per_kind = k.groupby(["kind", "category"]).agg(
        injected=("detected", "size"), detected=("detected", "sum"),
        median_delay_h=("delay_h", "median"),
        detector=("detector", lambda s: s.dropna().mode().iloc[0] if s.notna().any() else None),
    ).reset_index()
    per_kind["recall"] = per_kind["detected"] / per_kind["injected"]
    fa = pd.DataFrame(fa_rows)
    fa["per_week"] = fa["false_alarms"] / fa["weeks"]
    return EvalResult(per_kind=per_kind, false_alarms=fa,
                      alerts=pd.concat(all_alerts, ignore_index=True))


def to_markdown(r: EvalResult) -> str:
    pk = r.per_kind.copy()
    pk["recall"] = (pk["recall"] * 100).round().astype(int).astype(str) + " %"
    pk["median_delay_h"] = pk["median_delay_h"].round(1)
    lines = ["| anomaly | category | detected / injected | recall | median time to alert (h) | "
             "detector |", "|---|---|---|---|---|---|"]
    for _, x in pk.iterrows():
        lines.append(f"| {x['kind']} | {x['category']} | {x['detected']}/{x['injected']} | "
                     f"{x['recall']} | {x['median_delay_h']} | {x['detector']} |")
    fa = r.false_alarms
    lines += ["", "| run | weeks | false alarms | per week |", "|---|---|---|---|"]
    for _, x in fa.iterrows():
        lines.append(f"| {x['run']} | {x['weeks']:.0f} | {x['false_alarms']} | "
                     f"{x['per_week']:.2f} |")
    tot = fa["false_alarms"].sum() / fa["weeks"].sum()
    lines += ["", f"Overall: {tot:.2f} false alarms per week."]
    return "\n".join(lines)
