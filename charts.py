"""Chart generation — modern minimalist light theme."""
import io
from datetime import date

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.gridspec import GridSpec

# ── Palette ───────────────────────────────────────────────────────────────────
BG       = "#F4F5FB"   # soft lavender-white page bg
CARD     = "#FFFFFF"
PRIMARY  = "#7C3AED"   # purple
PRI_SOFT = "#EDE9FE"   # light purple fill
GREEN    = "#10B981"
GRN_SOFT = "#D1FAE5"
RED      = "#EF4444"
RED_SOFT = "#FEE2E2"
AMBER    = "#F59E0B"
TEXT1    = "#111827"
TEXT2    = "#6B7280"
GRID_C   = "#F3F4F6"
BORDER   = "#E5E7EB"
DOT_DONE = "#7C3AED"
DOT_FAIL = "#EF4444"
DOT_NONE = "#D1D5DB"


def _clean_ax(ax, title=""):
    """Apply clean light-theme styling to an axes."""
    ax.set_facecolor(CARD)
    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color(BORDER)
    ax.spines["bottom"].set_color(BORDER)
    ax.tick_params(colors=TEXT2, labelsize=8, length=0)
    ax.grid(axis="y", color=GRID_C, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    if title:
        ax.set_title(title, fontsize=10, fontweight="bold", color=TEXT1,
                     loc="left", pad=10)


def weekly_overview(members_data: list[dict], week_dates: list[date]) -> io.BytesIO:
    """
    members_data: [{name, daily_limit, daily_totals: {date: cal}, weight_history: [(date, kg)]}]
    week_dates: 7 date objects for the current week
    """
    n = len(members_data)
    today = date.today()
    day_labels = [d.strftime("%a") for d in week_dates]

    # Each member gets a row with 2 panels: cal chart (left) + weight (right)
    row_h = 3.6
    fig = plt.figure(figsize=(11, row_h * n + 0.8), facecolor=BG)

    week_label = f"{week_dates[0].strftime('%d %b')} – {week_dates[-1].strftime('%d %b %Y')}"
    fig.text(0.5, 0.99, f"Weekly Overview  ·  {week_label}",
             ha="center", va="top", fontsize=13, fontweight="bold", color=TEXT1)

    gs = GridSpec(n, 2, figure=fig, hspace=0.75, wspace=0.32,
                  left=0.07, right=0.97, top=0.96, bottom=0.02,
                  width_ratios=[3, 2])

    for i, m in enumerate(members_data):
        name    = m["name"]
        limit   = m.get("daily_limit", 2000)
        totals  = m.get("daily_totals", {})
        weights = m.get("weight_history", [])

        # ── Calorie bar chart ─────────────────────────────────────────────────
        ax = fig.add_subplot(gs[i, 0])

        bar_vals, bar_colors = [], []
        for d in week_dates:
            v = totals.get(d)
            if v is None:
                bar_vals.append(0)
                bar_colors.append(DOT_NONE)
            elif v > limit:
                bar_vals.append(v)
                bar_colors.append(RED)
            else:
                bar_vals.append(v)
                bar_colors.append(PRIMARY)

        bars = ax.bar(range(7), bar_vals, color=bar_colors, width=0.55,
                      zorder=3, linewidth=0)

        # Soft fill behind bars to show "space" up to limit
        ax.bar(range(7), [limit] * 7, color=PRI_SOFT, width=0.55,
               zorder=1, linewidth=0, alpha=0.35)

        # Limit dashed line
        ax.axhline(limit, color=AMBER, linewidth=1.5, linestyle="--",
                   alpha=0.9, zorder=4)
        ax.text(6.55, limit, f"{limit:,}", va="center", fontsize=7,
                color=AMBER, fontweight="bold")

        # Value labels on bars
        for j, (val, d) in enumerate(zip(bar_vals, week_dates)):
            orig = totals.get(d)
            if orig and orig > 0:
                label_y = val + limit * 0.025
                ax.text(j, label_y, f"{orig:,}", ha="center", va="bottom",
                        color=TEXT1, fontsize=6.5, fontweight="bold")

        # Streak dots just below x-axis
        for j, d in enumerate(week_dates):
            v = totals.get(d)
            is_future = d > today
            if is_future or v is None:
                dot_color, symbol = DOT_NONE, "○"
            elif v <= limit:
                dot_color, symbol = DOT_DONE, "●"
            else:
                dot_color, symbol = DOT_FAIL, "●"
            ax.annotate(symbol, xy=(j, 0),
                        xycoords=("data", "axes fraction"),
                        xytext=(0, -20), textcoords="offset points",
                        ha="center", fontsize=11, color=dot_color,
                        annotation_clip=False)

        days_ok = sum(1 for d in week_dates
                      if totals.get(d) is not None and totals[d] <= limit)
        streak_text = f"{days_ok}/7 days on track"

        _clean_ax(ax, f"{name}  ·  {streak_text}")
        ax.set_xlim(-0.5, 7.1)
        ax.set_ylim(0, max(max(bar_vals or [0]), limit) * 1.3)
        ax.set_xticks(range(7))
        ax.set_xticklabels(day_labels, fontsize=8.5, color=TEXT2)
        ax.set_ylabel("kcal", color=TEXT2, fontsize=8)
        ax.tick_params(axis="x", pad=14)   # room for streak dots

        # ── Weight chart ──────────────────────────────────────────────────────
        ax_w = fig.add_subplot(gs[i, 1])

        if weights:
            wdates = [w[0] for w in weights]
            wvals  = [w[1] for w in weights]

            lo = min(wvals)
            ax_w.fill_between(wdates, wvals, lo - 0.2,
                              alpha=0.12, color=PRIMARY, zorder=1)
            ax_w.plot(wdates, wvals, color=PRIMARY, linewidth=2.2,
                      marker="o", markersize=5,
                      markerfacecolor=CARD, markeredgecolor=PRIMARY,
                      markeredgewidth=2, zorder=3)

            # Latest label
            ax_w.annotate(f"{wvals[-1]:.1f} kg",
                          (wdates[-1], wvals[-1]),
                          textcoords="offset points", xytext=(-40, 9),
                          color=PRIMARY, fontsize=8.5, fontweight="bold")

            delta = wvals[-1] - wvals[0]
            arrow = "v" if delta < 0 else "^"
            d_color = GREEN if delta < 0 else RED
            ax_w.set_title("Weight",
                           fontsize=10, fontweight="bold", color=TEXT1,
                           loc="left", pad=10)
            ax_w.text(0.99, 1.02, f"{arrow} {abs(delta):.1f} kg",
                      transform=ax_w.transAxes, ha="right", va="bottom",
                      fontsize=9, fontweight="bold", color=d_color)

            ax_w.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
            ax_w.xaxis.set_major_locator(mdates.DayLocator(interval=2))
            plt.setp(ax_w.get_xticklabels(), rotation=30, ha="right",
                     fontsize=7.5, color=TEXT2)
            margin = max((max(wvals) - min(wvals)) * 0.4, 0.5)
            ax_w.set_ylim(lo - margin, max(wvals) + margin)
        else:
            ax_w.text(0.5, 0.5,
                      "No weight logged yet\nUse /weight to start",
                      ha="center", va="center", color=TEXT2, fontsize=8.5,
                      transform=ax_w.transAxes, linespacing=2)
            ax_w.set_title("Weight", fontsize=10, fontweight="bold",
                           color=TEXT1, loc="left", pad=10)

        _clean_ax(ax_w)
        ax_w.set_ylabel("kg", color=TEXT2, fontsize=8)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=BG, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf


def summary_chart(members_data: list[dict], since: date) -> io.BytesIO:
    """
    Leaderboard-style group summary.
    members_data: [{name, daily_limit, days_logged, days_over, total_debt, avg_cal, latest_weight}]
    """
    n = len(members_data)

    # Rank by days on track descending
    def _score(m):
        logged = max(m.get("days_logged", 0), 1)
        return (logged - m.get("days_over", 0)) / logged

    ranked = sorted(members_data, key=_score, reverse=True)

    fig_h = max(4.5, n * 0.9 + 3.2)
    fig, axes = plt.subplots(1, 3, figsize=(13, fig_h), facecolor=BG)
    fig.suptitle(f"Group Summary  —  Since {since.strftime('%d %b %Y')}",
                 fontsize=13, fontweight="bold", color=TEXT1, y=0.98)

    medals  = ["#1", "#2", "#3"]
    y       = list(range(n))
    names   = [m["name"] for m in ranked]
    ranked_labels = [
        f"{medals[i]}  {m['name']}" if i < 3 else f"      {m['name']}"
        for i, m in enumerate(ranked)
    ]

    # ── Panel 1: Days on track ─────────────────────────────────────────────
    ax = axes[0]
    days_logged = [m.get("days_logged", 0) for m in ranked]
    days_over   = [m.get("days_over", 0)   for m in ranked]
    days_ok     = [max(l - o, 0) for l, o in zip(days_logged, days_over)]

    ax.barh(y, days_ok,  color=PRIMARY,  height=0.5, zorder=3, linewidth=0)
    ax.barh(y, days_over, left=days_ok, color=RED_SOFT, height=0.5,
            zorder=3, linewidth=0)

    for yi, (ok, ov) in enumerate(zip(days_ok, days_over)):
        total = ok + ov or 1
        pct   = int(ok / total * 100)
        label = f"{pct}%  ✓{ok}"
        ax.text(ok + ov + 0.05, yi, label, va="center", color=TEXT2,
                fontsize=8)

    ax.set_yticks(y)
    ax.set_yticklabels(ranked_labels, fontsize=9, color=TEXT1)
    ax.set_xlabel("Days logged", color=TEXT2, fontsize=8)
    _clean_ax(ax, "On Track")

    # ── Panel 2: Avg daily calories ────────────────────────────────────────
    ax2 = axes[1]
    avg_cals = [m.get("avg_cal", 0)    for m in ranked]
    limits   = [m.get("daily_limit", 2000) for m in ranked]
    bar_clrs = [RED if a > l else PRIMARY for a, l in zip(avg_cals, limits)]

    ax2.barh(y, avg_cals, color=bar_clrs, height=0.5, zorder=3,
             alpha=0.85, linewidth=0)

    for yi, (val, lim) in enumerate(zip(avg_cals, limits)):
        ax2.axvline(lim, color=AMBER, linewidth=1.2, linestyle="--",
                    alpha=0.7, zorder=4)
        ax2.text(val + 15, yi, f"{val:.0f}", va="center",
                 color=TEXT2, fontsize=8)

    ax2.set_yticks(y)
    ax2.set_yticklabels(names, fontsize=9, color=TEXT1)
    ax2.set_xlabel("kcal / day avg", color=TEXT2, fontsize=8)
    _clean_ax(ax2, "Avg Calories")

    # ── Panel 3: Debt ──────────────────────────────────────────────────────
    ax3 = axes[2]
    debts    = [m.get("total_debt", 0) for m in ranked]
    d_colors = [RED if d > 0 else GREEN for d in debts]

    bars = ax3.barh(y, [max(d, 0.05) for d in debts],
                    color=d_colors, height=0.5, zorder=3,
                    alpha=0.85, linewidth=0)

    for bar, val in zip(bars, debts):
        if val > 0:
            label, color = f"${val:.2f}", TEXT1
        else:
            label, color = "✓ All clear", GREEN
        ax3.text(max(val + 0.08, 0.12),
                 bar.get_y() + bar.get_height() / 2,
                 label, va="center", color=color, fontsize=8,
                 fontweight="bold" if val == 0 else "normal")

    ax3.set_yticks(y)
    ax3.set_yticklabels(names, fontsize=9, color=TEXT1)
    ax3.set_xlabel("Amount owed ($)", color=TEXT2, fontsize=8)
    _clean_ax(ax3, "Debt")

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=BG, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf
