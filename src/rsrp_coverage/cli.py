"""Точка входа.

    python main.py --data data/07_coverage --out out

Пишет out/result.json в формате жюри, out/map.html и бонусный отчёт
out/bonus_day_night.json. Формат result.json бонусами не расширяется.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .cleaning import load_measurements
from .daynight import compare_day_night
from .mapping import render_map
from .pipeline import run

# Каталоги по умолчанию считаются от корня проекта, а не от текущего каталога,
# чтобы `python /полный/путь/main.py` работал откуда угодно. Пути, переданные
# явно через --data/--out, остаются относительными к текущему каталогу.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = PROJECT_ROOT / "data" / "07_coverage"
DEFAULT_OUT = PROJECT_ROOT / "out"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Карта покрытия и выбор площадок под новые базовые станции")
    parser.add_argument(
        "--data", default=str(DEFAULT_DATA),
        help="каталог с measurements.csv, towers.csv, candidates.csv, bounds.json")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="каталог для result.json, map.html и бонусных отчётов")
    parser.add_argument("--expected", default=None,
                        help="необязательный expected.json для самопроверки")
    args = parser.parse_args(argv)

    data_dir = Path(args.data)
    if not data_dir.exists():
        parser.error(f"Каталог с данными не найден: {data_dir}")
    for required in ("bounds.json", "measurements.csv", "towers.csv", "candidates.csv"):
        if not (data_dir / required).exists():
            parser.error(f"В каталоге {data_dir} нет файла {required}")

    result = run(args.data)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = result.to_result_json()
    (out_dir / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    render_map(result, out_dir / "map.html")

    # Бонус «день/ночь». Result не хранит сами замеры, поэтому CSV читается
    # ещё раз: это дешевле, чем расширять контракт основного прогона ради
    # бонуса. Очистка та же, значит и выборка та же.
    measurements, _ = load_measurements(data_dir / "measurements.csv", result.bounds)
    day_night = compare_day_night(measurements, result.bounds)
    bonus_path = out_dir / "bonus_day_night.json"
    bonus_path.write_text(
        json.dumps(day_night.to_json(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    reasons = json.dumps(result.report.as_dict()["by_reason"], ensure_ascii=False)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print()
    print(f"замеров принято      : {result.report.kept}")
    print(f"строк отброшено      : {result.report.dropped_rows}  {reasons}")
    print(f"клеток с замерами    : {result.cells_with_measurements}")
    print(f"клеток учтено (>=5)  : {len(result.cells)}")
    print(f"слабых (красных)     : {len(result.weak)}")
    print(f"допустимых площадок  : {len(result.eligible)} из {len(result.candidates)}")
    print(f"полный перебор       : {result.best_candidates} -> "
          f"{result.covered_weak_cells} слабых клеток")
    print(f"жадный алгоритм      : {result.greedy_candidates} -> "
          f"{result.greedy_covered} слабых клеток")
    print(f"карта                : {out_dir / 'map.html'}")
    print()
    print(f"{day_night.day.label:<21}: {day_night.day.as_line()}")
    print(f"{day_night.night.label:<21}: {day_night.night.as_line()}")
    print(f"красные только ночью : {len(day_night.weak_only_at_night)} клеток "
          f"(ещё {len(day_night.weak_at_night_not_counted_at_day)} ночью красные, "
          f"а днём не учтены)")
    print(f"красные только днём  : {len(day_night.weak_only_at_day)} клеток "
          f"(ещё {len(day_night.weak_at_day_not_counted_at_night)} днём красные, "
          f"а ночью не учтены)")
    print(f"бонус день/ночь      : {bonus_path}")

    if args.expected:
        expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
        matches = payload == expected
        print()
        print(f"сверка с эталоном    : {'СОВПАДАЕТ' if matches else 'РАСХОЖДЕНИЕ'}")
        if not matches:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
