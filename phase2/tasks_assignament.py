"""
Фаза 2. Распределение задач Фазы 3 между участниками команды (Pyomo).

Постановка (лексикографическая оптимизация, два шага):

  Множества:   I — назначаемые задачи Фазы 3;  J — участники (U1, U2, U3).
  Параметры:   T_i  — согласованная оценка трудозатрат задачи i, ч;
               C_ij — компетенция (1 — участник j может выполнять задачу i, 0 — не может);
               P_ij — предпочтение участника j к задаче i (1..5);
               L0_j — нагрузка участника j, уже набранная в Фазе 2, ч (факт, если заполнен, иначе план);
               G    — общая нагрузка на каждого от совместных встреч Фазы 3 (тип «общая»), ч.
  Переменные:  x_ij ∈ {0,1} — задача i назначена участнику j;  Z ≥ 0 — максимальная загрузка.

  Шаг 1:  min Z
          Σ_j x_ij = 1                          ∀i   (каждая задача — ровно одному)
          x_ij ≤ C_ij                           ∀i,j (только при наличии компетенции)
          L0_j + G + Σ_i T_i·x_ij ≤ Z           ∀j   (Z — верхняя граница загрузки каждого)
          x_{3.11,j} ≤ 2 − x_{3.09,j} − x_{3.10,j}  ∀j (проверку COSMIC делает не автор обоих подсчётов)

  Шаг 2:  max Σ_ij P_ij·x_ij  при тех же ограничениях и Z ≤ Z* + Δ,
          т.е. среди самых сбалансированных распределений выбираем то,
          которое лучше всего учитывает предпочтения участников.

Запуск:
  pip install pyomo highspy
  python распределение_задач.py                  # базовый вариант (с учётом нагрузки Фазы 2)
  python распределение_задач.py --no-phase2      # балансировать только Фазу 3
  python распределение_задач.py --delta 1        # допустить +1 ч к Z* ради предпочтений
"""

import argparse
import csv
import sys
from pathlib import Path

import pyomo.environ as pyo

# Чтение данных
TASKS_FILE = "data/фаза3_задачи_оценки.csv"
COMP_FILE = "data/матрица_компетенций.csv"
PREF_FILE = "data/матрица_предпочтений.csv"
PHASE2_FILE = "data/фаза2_мета_задачи.csv"

COL_AGREED = "Согласованная после сессии, ч"
COL_DRAFT = "Черновик согласованной (медиана), ч"
REVIEW_TASK, COUNT_TASKS = "3.11", ("3.09", "3.10")


def read_csv(path):
    # utf-8-sig — чтобы корректно читать файлы, сохранённые из Excel (с BOM)
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def num(value):
    """'2,5' / '2.5' / '' -> float или None."""
    value = (value or "").strip().replace(",", ".")
    return float(value) if value else None


def load_data(folder: Path, use_phase2: bool):
    tasks = read_csv(folder / TASKS_FILE)
    comp = read_csv(folder / COMP_FILE)
    pref = read_csv(folder / PREF_FILE)

    # Участники берутся из заголовка матрицы компетенций: "U1 (Менеджер)" -> id "U1", роль "Менеджер"
    person_cols = [c for c in comp[0].keys() if c.startswith("U")]
    people = [c.split()[0] for c in person_cols]
    roles = {c.split()[0]: c[c.find("(") + 1:c.rfind(")")] for c in person_cols}

    # Трудозатраты: берём согласованную оценку; если сессия ещё не проведена — черновик (медиану)
    T, names, used_draft = {}, {}, []
    common_hours = 0.0
    for row in tasks:
        hours = num(row.get(COL_AGREED))
        if hours is None:
            hours = num(row[COL_DRAFT])
            used_draft.append(row["ID"])
        if row["Тип"].strip() == "общая":
            common_hours += hours          # совместные встречи — константа для каждого
        else:
            T[row["ID"]] = hours
            names[row["ID"]] = row["Задача"]

    C = {(r["ID"], p): int(r[c]) for r in comp for p, c in zip(people, person_cols)}
    P = {(r["ID"], p): int(r[c]) for r in pref for p, c in zip(people, person_cols)}

    # Проверка согласованности файлов
    for i in T:
        for p in people:
            if (i, p) not in C or (i, p) not in P:
                sys.exit(f"Ошибка: для задачи {i} нет строки в матрице компетенций или предпочтений")
        if not any(C[i, p] for p in people):
            sys.exit(f"Ошибка: задачу {i} не может выполнить никто (все нули в матрице компетенций)")

    # Нагрузка Фазы 2: факт, если он уже внесён, иначе план
    L0 = {p: 0.0 for p in people}
    if use_phase2:
        for row in read_csv(folder / PHASE2_FILE):
            for p in people:
                fact = num(row.get(f"Факт {p}, ч"))
                L0[p] += fact if fact is not None else (num(row.get(f"План {p}, ч")) or 0.0)

    return dict(people=people, roles=roles, T=T, names=names, C=C, P=P,
                L0=L0, G=common_hours, used_draft=used_draft)


# Модель
def build_model(d):
    m = pyo.ConcreteModel("Распределение задач Фазы 3")

    m.I = pyo.Set(initialize=list(d["T"]), ordered=True)        # задачи
    m.J = pyo.Set(initialize=d["people"], ordered=True)         # участники

    m.T = pyo.Param(m.I, initialize=d["T"])
    m.C = pyo.Param(m.I, m.J, initialize=d["C"])
    m.P = pyo.Param(m.I, m.J, initialize=d["P"])
    m.L0 = pyo.Param(m.J, initialize=d["L0"])
    m.G = pyo.Param(initialize=d["G"])

    m.x = pyo.Var(m.I, m.J, domain=pyo.Binary)
    m.Z = pyo.Var(domain=pyo.NonNegativeReals)

    # Загрузка участника j (выражение, используется в ограничениях и в отчёте)
    m.workload = pyo.Expression(m.J, rule=lambda m, j:
                            m.L0[j] + m.G + sum(m.T[i] * m.x[i, j] for i in m.I))

    # Каждая задача назначается ровно одному участнику
    m.assign_once = pyo.Constraint(m.I, rule=lambda m, i: sum(m.x[i, j] for j in m.J) == 1)

    # Назначать можно только при наличии компетенции
    m.competence = pyo.Constraint(m.I, m.J, rule=lambda m, i, j: m.x[i, j] <= m.C[i, j])

    # Z не меньше загрузки каждого участника
    m.makespan = pyo.Constraint(m.J, rule=lambda m, j: m.workload[j] <= m.Z)

    # Независимая проверка COSMIC: если один человек сделал оба подсчёта, проверяет другой
    if REVIEW_TASK in d["T"] and all(t in d["T"] for t in COUNT_TASKS):
        a, b = COUNT_TASKS
        m.independent_review = pyo.Constraint(m.J, rule=lambda m, j:
                                              m.x[REVIEW_TASK, j] <= 2 - m.x[a, j] - m.x[b, j])

    # Две целевые функции; активной в каждый момент держим одну
    m.obj_makespan = pyo.Objective(expr=m.Z, sense=pyo.minimize)
    m.obj_pref = pyo.Objective(expr=sum(m.P[i, j] * m.x[i, j] for i in m.I for j in m.J),
                               sense=pyo.maximize)
    m.obj_pref.deactivate()
    return m


def get_solver(name):
    candidates = [name] if name else ["appsi_highs", "highs", "cbc", "glpk"]
    for s in candidates:
        solver = pyo.SolverFactory(s)
        if solver.available(exception_flag=False):
            return s, solver
    sys.exit("Не найден решатель. Установите: pip install highspy  (или GLPK/CBC)")


def solve(solver, m, step):
    res = solver.solve(m)
    tc = res.solver.termination_condition
    if tc != pyo.TerminationCondition.optimal:
        sys.exit(f"{step}: решение не найдено (termination condition = {tc})")
    return res


# Основной сценарий
def main():
    ap = argparse.ArgumentParser(description="Распределение задач Фазы 3 (Pyomo)")
    ap.add_argument("--data", default=".", help="папка с CSV-файлами")
    ap.add_argument("--no-phase2", action="store_true", help="не учитывать нагрузку Фазы 2")
    ap.add_argument("--delta", type=float, default=0.0, help="допуск к Z* на шаге 2, ч")
    ap.add_argument("--solver", default=None, help="appsi_highs | highs | cbc | glpk")
    ap.add_argument("--out", default="распределение_фаза3.csv", help="файл с результатом")
    args = ap.parse_args()

    d = load_data(Path(args.data), use_phase2=not args.no_phase2)
    solver_name, solver = get_solver(args.solver)

    print("=" * 70)
    print("ИСХОДНЫЕ ДАННЫЕ")
    print(f"Назначаемых задач: {len(d['T'])}, сумма T_i = {sum(d['T'].values()):g} ч")
    print(f"Общие встречи Фазы 3 (G): {d['G']:g} ч на каждого")
    print("Нагрузка Фазы 2 (L0): " + (", ".join(f"{p} = {d['L0'][p]:g} ч" for p in d["people"])
                                      if not args.no_phase2 else "не учитывается"))
    if d["used_draft"]:
        print(f"ВНИМАНИЕ: согласованная оценка не заполнена для {', '.join(d['used_draft'])} — "
              f"взят черновик (медиана)")
    total = sum(d["T"].values()) + sum(d["L0"].values()) + d["G"] * len(d["people"])
    print(f"Нижняя граница Z (идеальный баланс): {total / len(d['people']):.2f} ч")
    print(f"Решатель: {solver_name}")

    m = build_model(d)

    # --- Шаг 1: минимизация максимальной загрузки ---
    solve(solver, m, "Шаг 1")
    z_star = pyo.value(m.Z)
    pref_step1 = pyo.value(m.obj_pref.expr)
    print("=" * 70)
    print(f"ШАГ 1. min Z:  Z* = {z_star:g} ч  (сумма предпочтений в найденном решении = {pref_step1:g})")

    # --- Шаг 2: максимизация предпочтений при Z ≤ Z* + Δ ---
    m.obj_makespan.deactivate()
    m.obj_pref.activate()
    m.z_cap = pyo.Constraint(expr=m.Z <= z_star + args.delta + 1e-6)
    solve(solver, m, "Шаг 2")
    print(f"ШАГ 2. max ΣP при Z ≤ {z_star + args.delta:g}:  сумма предпочтений = "
          f"{pyo.value(m.obj_pref):g} (было {pref_step1:g}), "
          f"макс. из возможных = {sum(max(d['P'][i, p] for p in d['people'] if d['C'][i, p]) for i in d['T'])}")

    # --- Результат ---
    assign = {i: next(j for j in m.J if pyo.value(m.x[i, j]) > 0.5) for i in m.I}

    print("=" * 70)
    print("РАСПРЕДЕЛЕНИЕ")
    for i in m.I:
        j = assign[i]
        print(f"  {i:<5} {j}  T={d['T'][i]:>4g} ч  P={d['P'][i, j]}  {d['names'][i][:60]}")

    print("=" * 70)
    print("ЗАГРУЗКА УЧАСТНИКОВ")
    for j in m.J:
        mine = [i for i in m.I if assign[i] == j]
        own = sum(d["T"][i] for i in mine)
        avg_p = sum(d["P"][i, j] for i in mine) / len(mine) if mine else 0
        print(f"  {j} ({d['roles'][j]}): задач {len(mine):>2}, назначено {own:>5g} ч, "
              f"всего {pyo.value(m.workload[j]):>5g} ч (Ф2 {d['L0'][j]:g} + встречи {d['G']:g} + задачи {own:g}), "
              f"ср. предпочтение {avg_p:.2f}")

    out = Path(args.data) / args.out
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ID", "Задача", "Исполнитель (R)", "Роль", "Трудозатраты, ч", "Предпочтение"])
        for i in m.I:
            j = assign[i]
            w.writerow([i, d["names"][i], j, d["roles"][j], d["T"][i], d["P"][i, j]])
    print("=" * 70)
    print(f"Результат сохранён: {out}")


if __name__ == "__main__":
    main()
