#!/usr/bin/env python3
"""Единственная точка запуска решения ТЗ 7 «Карта покрытия».

    python main.py
    python main.py --data data/07_coverage --out out
    python main.py --expected data/07_coverage/expected.json

Стандартная библиотека Python 3.10+, сторонних зависимостей нет.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from rsrp_coverage.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
