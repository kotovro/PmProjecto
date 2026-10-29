"""
Фаза 4. Прогноз срока выполнения Фазы 5 методом Монте-Карло.

Модель 1 (по числу задач):
    из истории берём, сколько задач закрывалось в каждую неделю, и считаем это
    дискретным распределением. На каждой итерации сэмплируем недели, пока сумма
    закрытых задач не покроет все задачи Фазы 5.

Модель 2 (по трудозатратам):
    из истории берём, сколько человеко-часов команда тратила в каждую неделю.
    На каждой итерации сэмплируем длительность каждой задачи Фазы 5 из
    треугольного распределения (O, M, P), суммируем и сэмплируем недели, пока
    накопленные часы не покроют сумму.

Срок с 80% достоверностью = 80-й перцентиль полученного распределения (в неделях).

Запуск:
    python монте_карло.py                                   # data/history.csv
    python монте_карло.py --history data/history.csv --runs 100000 --seed 42
    python монте_карло.py --drop-zero-weeks                 # исключить недели без работы

Входные файлы:
    data/phase5_tasks.csv  — id, task, assignee, o, m, p (человеко-часы)
    data/history.csv       — phase, task, assignee, start, end, hours
                             (даты в формате ГГГГ-ММ-ДД, hours — фактические чел.-ч)
"""

import argparse
import csv
import datetime as dt
from collections import Counter
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt

BASE = Path(__file__).resolve().parent
PERCENTILES = (50, 80, 90)


# ---------------------------------------------------------------- чтение данных

def to_float(text):
    return float(str(text).strip().replace(",", "."))


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_phase5_tasks(path):
    tasks = []
    for row in read_csv(path):
        o, m, p = to_float(row["o"]), to_float(row["m"]), to_float(row["p"])
        if not o <= m <= p:
            raise ValueError(f"{row['id']}: нужно O ≤ M ≤ P, получено {o}/{m}/{p}")
        tasks.append({"id": row["id"], "task": row["task"], "o": o, "m": m, "p": p})
    return tasks


def load_history(path):
    tasks = []
    for row in read_csv(path):
        start = dt.date.fromisoformat(row["start"].strip())
        end = dt.date.fromisoformat(row["end"].strip())
        if end < start:
            raise ValueError(f"«{row['task']}»: дата окончания раньше даты начала")
        tasks.append({"task": row["task"], "start": start, "end": end,
                      "hours": to_float(row["hours"])})
    if not tasks:
        raise ValueError("История пуста")
    return tasks


# недельная статистика

def monday(day):
    return day - dt.timedelta(days=day.weekday())


def weekly_stats(history):
    """Недели — календарные (пн–вс), от первой до последней недели истории.
    Задача засчитывается в неделю, где она закрыта (end).
    Часы задачи распределяются равномерно по дням от start до end,
    поэтому длинная задача делит часы между неделями."""
    first = monday(min(t["start"] for t in history))
    last = monday(max(t["end"] for t in history))
    weeks = []
    w = first
    while w <= last:
        weeks.append(w)
        w += dt.timedelta(days=7)
    idx = {w: i for i, w in enumerate(weeks)}

    done = np.zeros(len(weeks), dtype=int)
    hours = np.zeros(len(weeks))
    for t in history:
        done[idx[monday(t["end"])]] += 1
        n_days = (t["end"] - t["start"]).days + 1
        per_day = t["hours"] / n_days
        for k in range(n_days):
            day = t["start"] + dt.timedelta(days=k)
            hours[idx[monday(day)]] += per_day
    return weeks, done, hours


# симуляция

def weeks_to_cover(targets, weekly_values, rng):
    """Для каждой цели сколько недель нужно, чтобы сумма случайно выбранных
    недельных значений достигла цели. Векторизовано по итерациям."""
    values = np.asarray(weekly_values, dtype=float)
    if values.max() <= 0:
        raise ValueError("Во всех неделях истории нулевая производительность")
    n = len(targets)
    acc = np.zeros(n)
    weeks = np.zeros(n, dtype=int)
    active = np.ones(n, dtype=bool)
    guard = 0
    while active.any():
        k = active.sum()
        acc[active] += rng.choice(values, size=k)
        weeks[active] += 1
        active = acc < targets
        guard += 1
        if guard > 10_000:
            raise RuntimeError("Симуляция не сходится — проверьте данные истории")
    return weeks


def model1(n_tasks, done_per_week, runs, rng):
    return weeks_to_cover(np.full(runs, n_tasks, dtype=float), done_per_week, rng)


def model2(tasks, hours_per_week, runs, rng):
    o = np.array([t["o"] for t in tasks])
    m = np.array([t["m"] for t in tasks])
    p = np.array([t["p"] for t in tasks])
    durations = np.empty((runs, len(tasks)))
    for j in range(len(tasks)):
        if o[j] == p[j]: # вырожденная оценка — без разброса
            durations[:, j] = m[j]
        else:
            durations[:, j] = rng.triangular(o[j], m[j], p[j], size=runs)
    work = durations.sum(axis=1)
    return weeks_to_cover(work, hours_per_week, rng), work


# вывод

def distribution_table(weeks):
    counts = Counter(weeks.tolist())
    total = len(weeks)
    lines = ["  Недель | Доля, % | Накопл., %", "  -------+---------+-----------"]
    cum = 0
    for w in sorted(counts):
        share = counts[w] / total
        cum += share
        lines.append(f"  {w:>6} | {share * 100:7.2f} | {cum * 100:9.2f}")
    return "\n".join(lines)


def percentile_weeks(weeks, q):
    # для дискретных недель берём наименьшее целое, при котором накопленная доля ≥ q
    return int(np.ceil(np.percentile(weeks, q, method="inverted_cdf")))


def save_plots(results_dir, w1, w2, p80_1, p80_2):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return "matplotlib не установлен — графики пропущены"

    ink, muted, grid, bar = "#111111", "#6B6B6B", "#E0E0E0", "#3B6FB6"
    fig, axes = plt.subplots(1, 2, figsize=(11, 4), sharey=True)
    for ax, weeks, p80, title in (
        (axes[0], w1, p80_1, "Модель 1: число задач в неделю"),
        (axes[1], w2, p80_2, "Модель 2: человеко-часы в неделю"),
    ):
        counts = Counter(weeks.tolist())
        xs = sorted(counts)
        ys = [counts[x] / len(weeks) * 100 for x in xs]
        ax.bar(xs, ys, width=0.6, color=bar, zorder=2)
        ax.axvline(p80 + 0.4, color=ink, linewidth=1.2, linestyle="--", zorder=3)
        ax.text(p80 + 0.5, max(ys) * 0.95, f"80%: ≤ {p80} нед.", color=ink, fontsize=10)
        ax.set_title(title, color=ink, fontsize=12, loc="left")
        ax.set_xlabel("Срок Фазы 5, недель", color=muted)
        ax.set_xticks(xs)
        ax.grid(axis="y", color=grid, linewidth=0.8, zorder=0)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(grid)
        ax.tick_params(colors=muted)
    axes[0].set_ylabel("Доля прогонов, %", color=muted)
    fig.tight_layout()
    path = results_dir / "гистограммы.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return f"Графики: {path}"


def main():
    ap = argparse.ArgumentParser(description="Монте-Карло прогноз срока Фазы 5")
    ap.add_argument("--tasks", default=BASE / "data" / "phase5_tasks.csv", type=Path)
    ap.add_argument("--history", default=BASE / "data" / "history.csv", type=Path)
    ap.add_argument("--results", default=BASE / "results", type=Path)
    ap.add_argument("--runs", default=100_000, type=int)
    ap.add_argument("--seed", default=42, type=int)
    ap.add_argument("--drop-zero-weeks", action="store_true",
                    help="не учитывать недели, когда не было закрыто ни одной задачи / потрачено 0 ч")
    args = ap.parse_args()

    if not args.history.exists():
        raise SystemExit(f"Нет файла истории: {args.history}\n")

    tasks = load_phase5_tasks(args.tasks)
    history = load_history(args.history)
    weeks, done, hours = weekly_stats(history)

    done_sample, hours_sample = done, hours
    if args.drop_zero_weeks:
        done_sample, hours_sample = done[done > 0], hours[hours > 0]

    rng = np.random.default_rng(args.seed)
    w1 = model1(len(tasks), done_sample, args.runs, rng)
    w2, work = model2(tasks, hours_sample, args.runs, rng)

    out = []
    out.append("=" * 64)
    out.append("ФАЗА 4. Монте-Карло прогноз срока Фазы 5")
    out.append(f"Прогонов: {args.runs}, seed: {args.seed}, "
               f"нулевые недели: {'исключены' if args.drop_zero_weeks else 'учтены'}")
    out.append("=" * 64)

    out.append("\nИстория по неделям (Фазы 1–3):")
    out.append("  Неделя с   | Закрыто задач | Чел.-ч")
    for w, d, h in zip(weeks, done, hours):
        out.append(f"  {w.isoformat()} | {d:>13} | {h:6.1f}")
    out.append(f"  Итого: {done.sum()} задач, {hours.sum():.1f} чел.-ч за {len(weeks)} нед.")
    out.append(f"  Среднее: {done_sample.mean():.2f} задач/нед, "
               f"{hours_sample.mean():.1f} чел.-ч/нед")

    out.append(f"\nЗадачи Фазы 5: {len(tasks)} шт.")
    out.append(f"  Σ O = {sum(t['o'] for t in tasks):.2f}, Σ M = {sum(t['m'] for t in tasks):.2f}, "
               f"Σ P = {sum(t['p'] for t in tasks):.2f} чел.-ч")
    out.append(f"  Σ ожидаемых (O+M+P)/3 = {sum((t['o'] + t['m'] + t['p']) / 3 for t in tasks):.2f} чел.-ч")
    out.append(f"  Симулированные трудозатраты: P10 = {np.percentile(work, 10):.1f}, "
               f"P50 = {np.percentile(work, 50):.1f}, P90 = {np.percentile(work, 90):.1f} чел.-ч")

    p1 = {q: percentile_weeks(w1, q) for q in PERCENTILES}
    p2 = {q: percentile_weeks(w2, q) for q in PERCENTILES}

    out.append("\n--- Модель 1: число задач в неделю ---")
    out.append(distribution_table(w1))
    out.append("  " + ", ".join(f"P{q} = {p1[q]} нед." for q in PERCENTILES))

    out.append("\n--- Модель 2: человеко-часы в неделю + треугольное распределение ---")
    out.append(distribution_table(w2))
    out.append("  " + ", ".join(f"P{q} = {p2[q]} нед." for q in PERCENTILES))

    out.append("\n" + "=" * 64)
    out.append(f"ИТОГ (80% достоверность): модель 1 — {p1[80]} нед., модель 2 — {p2[80]} нед.")
    out.append("=" * 64)

    args.results.mkdir(parents=True, exist_ok=True)
    out.append(save_plots(args.results, w1, w2, p1[80], p2[80]))

    text = "\n".join(out)
    print(text)
    suffix = "_без_нулевых" if args.drop_zero_weeks else ""
    log = args.results / f"прогноз{suffix}.txt"
    log.write_text(text, encoding="utf-8")
    print(f"Лог: {log}")


if __name__ == "__main__":
    main()
