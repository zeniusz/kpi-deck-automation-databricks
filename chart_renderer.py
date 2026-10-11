from __future__ import annotations

from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import pandas as pd


def normalize_data(rows: Iterable | pd.DataFrame) -> pd.DataFrame:
    if isinstance(rows, pd.DataFrame):
        df = rows.copy()
    else:
        df = pd.DataFrame(list(rows), columns=["label", "value"])
    if df.shape[1] < 2:
        raise ValueError("Chart data must have at least two columns: label and value")
    df = df.iloc[:, :2]
    df.columns = ["label", "value"]
    df["label"] = df["label"].astype(str)
    df["value"] = pd.to_numeric(df["value"], errors="coerce").fillna(0)
    return df


def render_chart(chart: dict, rows: Iterable | pd.DataFrame, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    df = normalize_data(rows)
    kind = chart.get("kind", "barh")
    path = output_dir / f"{chart['key']}.png"

    # Most charts are 16:9-ish. Donuts may opt into a square canvas so the guild fills a square
    # PowerPoint card rather than becoming a tiny chart inside a very wide dashboard screenshot.
    fig, ax = plt.subplots(figsize=chart.get("figsize", (7.4, 4.2)), dpi=180)

    if kind == "bar":
        ax.bar(df["label"], df["value"])
        ax.tick_params(axis="x", labelrotation=30, labelsize=8)
        ax.tick_params(axis="y", labelsize=8)
        ax.grid(axis="y", alpha=0.25)
    elif kind == "donut":
        values = df["value"].tolist()
        labels = df["label"].tolist()
        palette = ["#008AA6", "#E4003B", "#A23B61", "#F6A800", "#00876C",
                   "#79C5E1", "#70A18D", "#D9A6B8", "#7C8690"]

        def pct_label(pct: float) -> str:
            return f"{pct:.0f}%" if pct >= chart.get("donut_min_percent", 3) else ""

        wedges, _, auto = ax.pie(
            values, labels=None, autopct=pct_label, startangle=90, pctdistance=0.78,
            colors=palette[:len(values)], wedgeprops={"width": 0.42, "edgecolor": "white"},
            textprops={"fontsize": 9},
        )
        for label in auto:
            label.set_color("white")
            label.set_weight("bold")
        if chart.get("donut_legend", True):
            ax.legend(
                wedges, labels, loc="center left", bbox_to_anchor=(0.93, 0.5),
                frameon=False, fontsize=8,
            )
        ax.axis("equal")
    else:
        df = df.sort_values("value", ascending=True)
        ax.barh(df["label"], df["value"])
        ax.tick_params(axis="x", labelsize=8)
        ax.tick_params(axis="y", labelsize=8)
        ax.grid(axis="x", alpha=0.25)

    if chart.get("show_title", True):
        ax.set_title(chart.get("title", chart["key"]), fontsize=11, weight="bold", pad=10)
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight", transparent=False)
    plt.close(fig)
    return path
