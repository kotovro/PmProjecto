# -*- coding: utf-8 -*-
"""
Фаза 5. Планирование проекта «Телеграм-бот агрегации расписаний и рассылок
из трёх источников» (API ВУЗа, сайт концертного зала, RSS новостного портала).

Что делает скрипт:
  1. Содержит декомпозицию проекта на 25 задач: длительность (ч),
     ресурсные требования по специальностям, связи предшествования.
  2. Считает сетевую модель без учёта ресурсов (CPM): ES, EF, LS, LF,
     общий и свободный резервы.
  3. Строит расписание с учётом ограниченных (возобновимых) ресурсов
     последовательной схемой генерации расписания (Serial SGS) по каждому
     из 12 правил приоритета (SLK, FREE, LST, LFT, LSTLFT, GRPW, LPT, MIS,
     GRD, GRWC, GCRWC, ROT) + N случайных приоритетов.
  4. Выбирает лучшее решение (минимальная длительность проекта, makespan).
  5. Накладывает его на рабочий календарь 8 ч/день, 40 ч/неделю
     (пн–пт, 09:00–18:00, обед 13:00–14:00, с учётом праздников)
     и выводит плановые даты задач и этапов.

Запуск:
  python phase5_scheduling.py
  python phase5_scheduling.py --start 2026-11-09 --random 2000 --seed 7
  python phase5_scheduling.py --rule LFT       # подробно показать конкретную эвристику
  python phase5_scheduling.py --no-plot        # без диаграммы Ганта

Зависимости: только стандартная библиотека; matplotlib — опционально (Гант).
"""

import argparse
import csv
import os
import random
from datetime import date, datetime, time, timedelta

# ---------------------------------------------------------------------------
# 1. ИСХОДНЫЕ ДАННЫЕ
# ---------------------------------------------------------------------------

# Типы ресурсов и их запасы (сколько людей каждой специальности в команде).
# Состав взят из вывода Фазы 3 (п. 6.2): backend-разработчик, дата-инженер,
# инженер по тестированию — по одному человеку.
RESOURCES = {
    "BE": 1,  # Backend-разработчик (Python, aiogram/asyncio, БД, рассылки)
    "DE": 1,  # Дата-инженер (HTTP, JSON/HTML/XML-парсинг, планировщик)
    "QA": 1,  # Инженер по тестированию
}
RES_NAMES = {"BE": "Backend-разработчик", "DE": "Дата-инженер", "QA": "Тестировщик"}

# Этапы проекта (для сводки плановых сроков по этапам).
STAGES = {
    "S1": "Подготовка и архитектура",
    "S2": "Ядро бота и пользовательские сценарии",
    "S3": "Сбор данных из источников",
    "S4": "Рассылки",
    "S5": "Тестирование",
    "S6": "Стабилизация и запуск",
}

# Задачи: id -> (название, этап, длительность в рабочих часах,
#                {ресурс: кол-во людей}, [предшественники])
# Длительности опираются на консенсус покера планирования из Фазы 3
# (UC01..UC12 = 56 ч) + инфраструктурные задачи, тестирование и запуск.
TASKS = {
    "T01": ("Репозиторий, окружение, CI, получение токенов бота и API ВУЗа", "S1", 3, {"BE": 1}, []),
    "T02": ("Проектирование архитектуры и схемы БД (пользователи, подписки, события, журнал)", "S1", 4, {"BE": 1, "DE": 1}, []),
    "T03": ("Модели БД и слой доступа к данным (миграции, репозитории)", "S1", 4, {"BE": 1}, ["T01", "T02"]),
    "T04": ("Каркас бота: aiogram, роутинг команд, клавиатуры, обработчик ошибок", "S2", 3, {"BE": 1}, ["T01"]),
    "T05": ("UC01 Регистрация (/start)", "S2", 2, {"BE": 1}, ["T03", "T04"]),
    "T06": ("UC06 Справка (/help)", "S2", 1, {"BE": 1}, ["T04"]),
    "T07": ("UC02 Подписка на источник", "S2", 4, {"BE": 1}, ["T05"]),
    "T08": ("UC03 Отписка от источника", "S2", 2, {"BE": 1}, ["T07"]),
    "T09": ("UC05 Просмотр подписок и фильтров", "S2", 3, {"BE": 1}, ["T07"]),
    "T10": ("UC04 Настройка фильтров (группа, жанр, тег; сброс)", "S2", 8, {"BE": 1}, ["T07"]),
    "T11": ("Планировщик фоновых задач (APScheduler: 15 мин / 1 ч / сутки)", "S3", 2, {"DE": 1}, ["T01", "T02"]),
    "T12": ("Общий каркас парсеров: интерфейс источника, HTTP-клиент, ретраи, таймауты", "S3", 3, {"DE": 1}, ["T02"]),
    "T13": ("UC07 Сбор данных из API ВУЗа (REST/JSON, токен, 401)", "S3", 5, {"DE": 1}, ["T03", "T12"]),
    "T14": ("UC08 Парсинг сайта концертного зала (HTML, CSS-селекторы)", "S3", 8, {"DE": 1}, ["T03", "T12"]),
    "T15": ("UC09 Парсинг RSS-ленты новостей (XML, RSS 2.0)", "S3", 4, {"DE": 1}, ["T03", "T12"]),
    "T16": ("UC11 Обработка сбоев источников, журнал ошибок, алерты админу", "S3", 5, {"DE": 1}, ["T13", "T14", "T15"]),
    "T17": ("UC10 Ежедневная рассылка (сборка за сутки, фильтры, лимиты Telegram)", "S4", 6, {"BE": 1, "DE": 1}, ["T10", "T11", "T13", "T14", "T15"]),
    "T18": ("UC12 Глобальная рассылка и права администратора", "S4", 4, {"BE": 1}, ["T05"]),
    "T19": ("Тест-план и тест-кейсы (в т.ч. негативные сценарии)", "S5", 4, {"QA": 1}, ["T02"]),
    "T20": ("Моки/стабы внешних источников (таймауты, смена вёрстки, битый XML)", "S5", 3, {"QA": 1, "DE": 1}, ["T12", "T19"]),
    "T21": ("Тестирование пользовательских сценариев UC01–UC06", "S5", 4, {"QA": 1}, ["T06", "T08", "T09", "T10", "T19"]),
    "T22": ("Тестирование сбора данных и сбоев UC07–UC09, UC11", "S5", 4, {"QA": 1}, ["T16", "T20"]),
    "T23": ("Интеграционное тестирование рассылок UC10, UC12", "S5", 4, {"QA": 1, "BE": 1}, ["T17", "T18", "T21", "T22"]),
    "T24": ("Исправление дефектов по результатам тестирования", "S6", 6, {"BE": 1, "DE": 1}, ["T23"]),
    "T25": ("Деплой на VPS, мониторинг, README и инструкция администратора", "S6", 3, {"BE": 1, "DE": 1}, ["T24"]),
}

# Календарь
WORKDAY_START = time(9, 0)
LUNCH_AFTER_HOURS = 4          # обед после 4-го рабочего часа (13:00–14:00)
HOURS_PER_DAY = 8
# Нерабочие праздничные дни РФ (дополните при необходимости)
HOLIDAYS = {
    date(2026, 11, 4),
    *(date(2026, 12, 31) + timedelta(days=i) for i in range(0, 9)),  # 31.12–08.01
    date(2027, 2, 23), date(2027, 3, 8),
}

# ---------------------------------------------------------------------------
# 2. СЕТЕВАЯ МОДЕЛЬ (CPM без учёта ресурсов)
# ---------------------------------------------------------------------------


def build_graph(tasks):
    ids = list(tasks)
    dur = {j: tasks[j][2] for j in ids}
    req = {j: tasks[j][3] for j in ids}
    preds = {j: list(tasks[j][4]) for j in ids}
    succs = {j: [] for j in ids}
    for j in ids:
        for p in preds[j]:
            if p not in tasks:
                raise ValueError(f"{j}: неизвестный предшественник {p}")
            succs[p].append(j)
    for j in ids:
        for k, q in req[j].items():
            if k not in RESOURCES:
                raise ValueError(f"{j}: неизвестный тип ресурса {k}")
            if q > RESOURCES[k]:
                raise ValueError(f"{j}: требуется {q} {k}, а в наличии {RESOURCES[k]}")
    return ids, dur, req, preds, succs


def topo_order(ids, preds):
    order, done = [], set()
    pending = list(ids)
    while pending:
        progressed = False
        for j in list(pending):
            if all(p in done for p in preds[j]):
                order.append(j); done.add(j); pending.remove(j); progressed = True
        if not progressed:
            raise ValueError(f"Цикл в графе предшествования: {pending}")
    return order


def cpm(ids, dur, preds, succs):
    order = topo_order(ids, preds)
    es, ef = {}, {}
    for j in order:
        es[j] = max((ef[p] for p in preds[j]), default=0)
        ef[j] = es[j] + dur[j]
    T = max(ef.values())
    lf, ls = {}, {}
    for j in reversed(order):
        lf[j] = min((ls[s] for s in succs[j]), default=T)
        ls[j] = lf[j] - dur[j]
    total_slack = {j: ls[j] - es[j] for j in ids}
    free_slack = {j: min((es[s] for s in succs[j]), default=T) - ef[j] for j in ids}
    return dict(ES=es, EF=ef, LS=ls, LF=lf, SLK=total_slack, FREE=free_slack, T=T)


# ---------------------------------------------------------------------------
# 3. ПРАВИЛА ПРИОРИТЕТА
#    Каждое правило возвращает словарь {задача: ключ}; МЕНЬШИЙ ключ = выше приоритет.
#    Правила «по убыванию» реализованы через знак минус.
#    При равенстве — меньший номер задачи (детерминированный tie-break).
# ---------------------------------------------------------------------------


def priority_rules(ids, dur, req, succs, c):
    sum_r = {j: sum(req[j].values()) for j in ids}
    return {
        "SLK":    {j: c["SLK"][j] for j in ids},                                   # ↑ общий резерв
        "FREE":   {j: c["FREE"][j] for j in ids},                                  # ↑ свободный резерв
        "LST":    {j: c["LS"][j] for j in ids},                                    # ↑ позднее начало
        "LFT":    {j: c["LF"][j] for j in ids},                                    # ↑ позднее окончание
        "LSTLFT": {j: c["LS"][j] + c["LF"][j] for j in ids},                       # ↑ LS + LF
        "GRPW":   {j: -(dur[j] + sum(dur[s] for s in succs[j])) for j in ids},     # ↓ d_j + Σ d прямых послед.
        "LPT":    {j: -dur[j] for j in ids},                                       # ↓ длительность
        "MIS":    {j: -len(succs[j]) for j in ids},                                # ↓ число прямых послед.
        "GRD":    {j: -dur[j] * sum_r[j] for j in ids},                            # ↓ d_j · Σ_k r_jk
        "GRWC":   {j: -sum_r[j] for j in ids},                                     # ↓ Σ_k r_jk
        "GCRWC":  {j: -(sum_r[j] + sum(sum_r[s] for s in succs[j])) for j in ids}, # ↓ Σ r_j + Σ r прямых послед.
        "ROT":    {j: -(sum(q / RESOURCES[k] for k, q in req[j].items()) / dur[j]) for j in ids},  # ↓ Σ(r_jk/R_k)/d_j
    }


# ---------------------------------------------------------------------------
# 4. ПОСЛЕДОВАТЕЛЬНАЯ СХЕМА ГЕНЕРАЦИИ РАСПИСАНИЯ (Serial SGS)
#    На каждом шаге из множества допустимых (все предшественники уже
#    поставлены) выбирается задача с наивысшим приоритетом и ставится
#    на самое раннее время, когда выполнены связи и хватает ресурсов
#    на всю её длительность.
# ---------------------------------------------------------------------------


def serial_sgs(ids, dur, req, preds, prio):
    horizon = sum(dur.values()) + 1
    usage = {k: [0] * horizon for k in RESOURCES}
    start, finish, activity_list = {}, {}, []
    idx = {j: i for i, j in enumerate(ids)}

    def fits(j, t):
        for k, q in req[j].items():
            u = usage[k]
            for h in range(t, t + dur[j]):
                if u[h] + q > RESOURCES[k]:
                    return False
        return True

    while len(start) < len(ids):
        eligible = [j for j in ids if j not in start and all(p in finish for p in preds[j])]
        j = min(eligible, key=lambda x: (prio[x], idx[x]))
        t = max((finish[p] for p in preds[j]), default=0)
        while not fits(j, t):
            t += 1
        for k, q in req[j].items():
            for h in range(t, t + dur[j]):
                usage[k][h] += q
        start[j], finish[j] = t, t + dur[j]
        activity_list.append(j)

    return dict(start=start, finish=finish, makespan=max(finish.values()), order=activity_list)


# ---------------------------------------------------------------------------
# 5. КАЛЕНДАРЬ: перевод «рабочих часов от начала проекта» в дату/время
# ---------------------------------------------------------------------------


def workdays(start_day):
    d = start_day
    while True:
        if d.weekday() < 5 and d not in HOLIDAYS:
            yield d
        d += timedelta(days=1)


def make_calendar(start_day, n_days):
    gen = workdays(start_day)
    return [next(gen) for _ in range(n_days + 1)]


def hour_to_clock(h_in_day):
    extra = 1 if h_in_day > LUNCH_AFTER_HOURS else 0  # обед
    return (datetime.combine(date.min, WORKDAY_START) + timedelta(hours=h_in_day + extra)).time()


def offset_to_dt(offset, cal, is_finish=False):
    """Смещение в рабочих часах -> datetime. Конец задачи, попадающий ровно на
    границу дня, относим к 18:00 предыдущего рабочего дня."""
    if is_finish and offset > 0 and offset % HOURS_PER_DAY == 0:
        d, h = offset // HOURS_PER_DAY - 1, HOURS_PER_DAY
    else:
        d, h = divmod(offset, HOURS_PER_DAY)
    if is_finish and h == LUNCH_AFTER_HOURS:
        return datetime.combine(cal[d], time(13, 0))
    return datetime.combine(cal[d], hour_to_clock(h))


# ---------------------------------------------------------------------------
# 6. ВЫВОД
# ---------------------------------------------------------------------------


WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def fmt(dt):
    return f"{WEEKDAYS_RU[dt.weekday()]} {dt:%d.%m %H:%M}"


def print_table(rows, headers):
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line); print("-" * len(line))
    for r in rows:
        print("  ".join(str(x).ljust(w) for x, w in zip(r, widths)))


def plot_gantt(ids, sched, req, cal, path, title):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib не установлен — диаграмма Ганта пропущена.")
        return
    colors = {"BE": "#4C78A8", "DE": "#F58518", "QA": "#54A24B"}
    fig, ax = plt.subplots(figsize=(14, 0.38 * len(ids) + 1.5))
    for i, j in enumerate(ids):
        s, f = sched["start"][j], sched["finish"][j]
        kinds = list(req[j])
        h = 0.8 / len(kinds)
        for n, k in enumerate(kinds):  # полоса делится по типам ресурсов задачи
            ax.barh(i - 0.4 + h * (n + 0.5), f - s, left=s, height=h, color=colors[k], edgecolor="white")
        ax.text(f + 0.3, i, j, va="center", fontsize=7)
    mk = sched["makespan"]
    for d in range(0, mk + HOURS_PER_DAY, HOURS_PER_DAY):
        ax.axvline(d, color="#ccc", lw=0.6, zorder=0)
    ax.set_xticks(range(0, mk + 1, HOURS_PER_DAY))
    ax.set_xticklabels([cal[d // HOURS_PER_DAY].strftime("%d.%m") for d in range(0, mk + 1, HOURS_PER_DAY)], fontsize=8)
    ax.set_yticks(range(len(ids)))
    ax.set_yticklabels([f"{j} {TASKS[j][0][:45]}" for j in ids], fontsize=7)
    ax.invert_yaxis()
    ax.set_xlabel("Рабочие дни (начало дня)")
    ax.set_title(title)
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=c, label=RES_NAMES[k]) for k, c in colors.items()], loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    print(f"Диаграмма Ганта: {path}")


def main():
    ap = argparse.ArgumentParser(description="Фаза 5: эвристическое планирование RCPSP")
    ap.add_argument("--start", default="2026-11-09", help="дата начала проекта, ГГГГ-ММ-ДД")
    ap.add_argument("--random", type=int, default=1000, help="число случайных приоритетов")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--rule", default=None, help="показать подробно эту эвристику вместо лучшей")
    ap.add_argument("--out", default="phase5_out", help="папка для CSV и диаграммы")
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()

    ids, dur, req, preds, succs = build_graph(TASKS)
    c = cpm(ids, dur, preds, succs)

    # --- CPM ---
    print(f"\nЗадач: {len(ids)}; ресурсы: " + ", ".join(f"{RES_NAMES[k]} ×{v}" for k, v in RESOURCES.items()))
    print(f"Трудоёмкость: {sum(dur[j] * sum(req[j].values()) for j in ids)} чел·ч; "
          f"критический путь без учёта ресурсов: {c['T']} ч\n")
    print_table([(j, dur[j], "+".join(f"{k}{'' if q == 1 else '×' + str(q)}" for k, q in req[j].items()),
                  ",".join(preds[j]) or "—", c["ES"][j], c["EF"][j], c["LS"][j], c["LF"][j],
                  c["SLK"][j], c["FREE"][j]) for j in ids],
                ["ID", "d", "Ресурсы", "Предш.", "ES", "EF", "LS", "LF", "SLK", "FREE"])

    # --- Эвристики ---
    results = {}
    for name, prio in priority_rules(ids, dur, req, succs, c).items():
        results[name] = serial_sgs(ids, dur, req, preds, prio)

    rnd = random.Random(args.seed)
    rand_spans = []
    best_rand = None
    for i in range(args.random):
        prio = {j: rnd.random() for j in ids}
        s = serial_sgs(ids, dur, req, preds, prio)
        rand_spans.append(s["makespan"])
        if best_rand is None or s["makespan"] < best_rand["makespan"]:
            best_rand, best_rand_i = s, i
    if best_rand is not None:
        results[f"RANDOM#{best_rand_i}"] = best_rand

    print("\nРезультаты эвристик (Serial SGS), makespan в рабочих часах / днях:")
    print_table([(n, r["makespan"], f"{r['makespan'] / HOURS_PER_DAY:.2f}") for n, r in
                 sorted(results.items(), key=lambda kv: kv[1]["makespan"])],
                ["Эвристика", "Часы", "Дни"])
    if rand_spans:
        print(f"Случайные ({len(rand_spans)} шт.): мин {min(rand_spans)}, "
              f"сред {sum(rand_spans) / len(rand_spans):.1f}, макс {max(rand_spans)} ч")

    if args.rule:
        if args.rule not in results:
            raise SystemExit(f"Нет эвристики {args.rule}. Доступны: {', '.join(results)}")
        chosen_name = args.rule
    else:
        # лучшая по makespan; при равенстве — детерминированное правило раньше случайного
        chosen_name = min(results, key=lambda n: (results[n]["makespan"], n.startswith("RANDOM")))
    best = results[chosen_name]
    tied = [n for n, r in results.items() if r["makespan"] == best["makespan"]]
    print(f"\nИтоговое решение: {chosen_name}, makespan = {best['makespan']} ч "
          f"(нижняя граница по критическому пути: {c['T']} ч)")
    print(f"Такой же результат у: {', '.join(tied)}")
    print("Порядок постановки задач (activity list):", " → ".join(best["order"]))

    # --- Календарь ---
    start_day = datetime.strptime(args.start, "%Y-%m-%d").date()
    cal = make_calendar(start_day, best["makespan"] // HOURS_PER_DAY + 2)
    print(f"\nПлан на календаре (8 ч/день, пн–пт, 09:00–18:00, обед 13–14), старт {start_day:%d.%m.%Y}:")
    rows = []
    for j in sorted(ids, key=lambda x: (best["start"][x], x)):
        s, f = best["start"][j], best["finish"][j]
        rows.append((j, TASKS[j][0][:60], "+".join(req[j]), s, f,
                     fmt(offset_to_dt(s, cal)), fmt(offset_to_dt(f, cal, True))))
    print_table(rows, ["ID", "Задача", "Ресурсы", "Старт,ч", "Финиш,ч", "Начало", "Окончание"])

    print("\nПлановые сроки этапов:")
    stage_rows = []
    for sid, sname in STAGES.items():
        js = [j for j in ids if TASKS[j][1] == sid]
        s = min(best["start"][j] for j in js); f = max(best["finish"][j] for j in js)
        stage_rows.append((sid, sname, fmt(offset_to_dt(s, cal)), fmt(offset_to_dt(f, cal, True))))
    print_table(stage_rows, ["Этап", "Название", "Начало", "Окончание"])

    finish_dt = offset_to_dt(best["makespan"], cal, True)
    n_days = -(-best["makespan"] // HOURS_PER_DAY)
    print(f"\nПроект: {fmt(offset_to_dt(0, cal))} — {fmt(finish_dt)}; "
          f"{n_days} раб. дн. ≈ {n_days / 5:.1f} раб. нед.")

    print("\nЗагрузка ресурсов:")
    for k, cap in RESOURCES.items():
        busy = sum(dur[j] * req[j].get(k, 0) for j in ids)
        print(f"  {RES_NAMES[k]:<22} {busy:>3} ч из {cap * best['makespan']} ч доступных "
              f"({busy / (cap * best['makespan']):.0%})")

    # --- Файлы ---
    os.makedirs(args.out, exist_ok=True)
    csv_path = os.path.join(args.out, "phase5_schedule.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["ID", "Задача", "Этап", "Длительность, ч", "Ресурсы", "Предшественники",
                    "ES", "EF", "LS", "LF", "SLK", "FREE", "Старт, ч", "Финиш, ч", "Начало", "Окончание"])
        for j in ids:
            w.writerow([j, TASKS[j][0], STAGES[TASKS[j][1]], dur[j],
                        ", ".join(f"{RES_NAMES[k]}×{q}" for k, q in req[j].items()), ", ".join(preds[j]),
                        c["ES"][j], c["EF"][j], c["LS"][j], c["LF"][j], c["SLK"][j], c["FREE"][j],
                        best["start"][j], best["finish"][j],
                        offset_to_dt(best["start"][j], cal).strftime("%d.%m.%Y %H:%M"),
                        offset_to_dt(best["finish"][j], cal, True).strftime("%d.%m.%Y %H:%M")])
    heur_path = os.path.join(args.out, "phase5_heuristics.csv")
    with open(heur_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Эвристика", "Makespan, ч", "Порядок задач"])
        for n, r in sorted(results.items(), key=lambda kv: kv[1]["makespan"]):
            w.writerow([n, r["makespan"], " ".join(r["order"])])
    print(f"\nCSV: {csv_path}, {heur_path}")

    if not args.no_plot:
        plot_gantt(ids, best, req, cal, os.path.join(args.out, "phase5_gantt.png"),
                   f"Диаграмма Ганта — {chosen_name}, {best['makespan']} ч")


if __name__ == "__main__":
    main()
