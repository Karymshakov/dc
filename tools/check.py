#!/usr/bin/env python3
"""Полная проверка решения одной командой: python tools/check.py

Прогоняет всё, на чём держится сдача, и печатает итог так, чтобы его
можно было показать не читая вывод целиком. Ненулевой код возврата
означает, что коммитить нельзя.

Порядок выбран от дешёвого к дорогому: контракт жюри проверяется первым,
потому что его поломка обесценивает всё остальное.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Пороги зачёта из ТЗ.
JURY_TOP1, JURY_TOP3, JURY_LANG = 0.75, 0.90, 0.90


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        mark = "OK  " if self.ok else "СБОЙ"
        return f"  [{mark}] {self.name}" + (f"  — {self.detail}" if self.detail else "")


def run(command: list[str]) -> tuple[bool, str]:
    finished = subprocess.run(command, cwd=PROJECT_ROOT,
                              capture_output=True, text=True)
    output = (finished.stdout or "") + (finished.stderr or "")
    return finished.returncode == 0, output


def last_json(output: str):
    """Последняя строка вывода, которая разбирается как JSON.

    Подпроцесс может напечатать предупреждение перед полезным выводом,
    поэтому берём не просто последнюю строку, а последнюю разбираемую.
    """
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"в выводе нет JSON: {output.strip()[-200:] or '(пусто)'}")


def check_contract() -> list[Step]:
    """Контракт жюри: формат ответа и работа без Telegram."""
    steps: list[Step] = []

    code = (
        "import importlib.util as u, sys, json;"
        "sys.path.insert(0, '.');"
        "from faq_bot.answer import answer;"
        "r = answer('Как проверить баланс?');"
        "print(json.dumps({'keys': sorted(r), 'faq_id': r.get('faq_id'),"
        "'telegram': u.find_spec('telegram') is not None}, ensure_ascii=False))"
    )
    ok, output = run([sys.executable, "-I", "-c", code])
    if not ok:
        return [Step("answer() импортируется и отвечает", False, output.strip()[-200:])]

    payload = last_json(output)
    expected = ["candidates", "confidence", "faq_id", "lang"]
    steps.append(Step("формат ответа ровно по ТЗ", payload["keys"] == expected,
                      f"получено {payload['keys']}" if payload["keys"] != expected else ""))
    steps.append(Step("тема находится", payload["faq_id"] == 1))

    # Пустой вход и мусор не должны ронять функцию. Скрипт пишется во
    # временный файл: многострочный код через -c ломается на отступах.
    script = PROJECT_ROOT / ".check_inputs.py"
    script.write_text(
        "import sys, json\n"
        "sys.path.insert(0, '.')\n"
        "from faq_bot.answer import answer\n"
        "bad = ['', '   ', None, 0, [], 123, 3.5, b'x', '\\x00']\n"
        "res = []\n"
        "for item in bad:\n"
        "    try:\n"
        "        value = answer(item)\n"
        "        res.append(isinstance(value, dict)\n"
        "                   and 0.0 <= value['confidence'] <= 1.0)\n"
        "    except Exception as error:\n"
        "        res.append(f'{type(item).__name__}: {error}')\n"
        "print(json.dumps(res, ensure_ascii=False))\n",
        encoding="utf-8",
    )
    try:
        ok, output = run([sys.executable, "-I", str(script)])
    finally:
        script.unlink(missing_ok=True)
    failures = []
    if ok:
        results = last_json(output)
        failures = [item for item in results if item is not True]
    steps.append(Step("ни один вход не роняет answer()", ok and not failures,
                      "; ".join(str(item) for item in failures[:3])))
    return steps


def check_accuracy() -> list[Step]:
    """Точность против порогов зачёта."""
    code = (
        "import sys; sys.path.insert(0, '.');"
        "sys.argv = ['evaluate', '--split', 'tune'];"
        "import runpy; runpy.run_path('tools/evaluate.py', run_name='__main__')"
    )
    ok, output = run([sys.executable, "-I", "-c", code])
    if not ok:
        return [Step("прогон точности", False, output.strip()[-200:])]

    steps: list[Step] = []
    for label, threshold, key in (("top-1", JURY_TOP1, "top-1"),
                                  ("top-3", JURY_TOP3, "top-3"),
                                  ("язык", JURY_LANG, "язык")):
        line = next((row for row in output.splitlines() if key in row), "")
        share = 0.0
        for token in line.split():
            if token.endswith("%"):
                try:
                    share = float(token.rstrip("%")) / 100
                except ValueError:
                    pass
                break
        steps.append(Step(f"{label} на tune не ниже {threshold:.0%}",
                          share >= threshold, f"{share:.1%}"))
    return steps


def check_tests() -> list[Step]:
    ok, output = run([sys.executable, "-m", "pytest", "-q"])
    summary = next((row for row in reversed(output.splitlines()) if "passed" in row
                    or "failed" in row), "нет вывода")
    return [Step("тесты", ok, summary.strip())]


def check_consistency() -> list[Step]:
    """Сторож фактов ловит подмену привязки."""
    code = (
        "import sys; sys.path.insert(0, '.');"
        "from faq_bot.consistency import check;"
        "from faq_bot.knowledge import load_faq;"
        "entry = [e for e in load_faq() if e.id == 1][0];"
        "print(int(check('Наберите *111#.', entry).consistent),"
        "      int(check(entry.answer_ru, entry).consistent))"
    )
    ok, output = run([sys.executable, "-I", "-c", code])
    if not ok:
        return [Step("сторож фактов", False, output.strip()[-160:])]
    wrong, right = output.strip().split()
    return [Step("чужой факт отклоняется, свой проходит",
                 wrong == "0" and right == "1")]


def check_no_secrets() -> list[Step]:
    """Ключи не должны попасть в репозиторий."""
    ok, output = run(["git", "ls-files"])
    tracked = output.split()
    leaked = [name for name in tracked if name in (".env", ".env.local")]
    steps = [Step(".env не отслеживается git", not leaked, ", ".join(leaked))]

    ok, output = run(["git", "grep", "-lE", r"[0-9]{8,}:[A-Za-z0-9_-]{30,}"])
    # git grep возвращает 1, когда ничего не найдено — это успех.
    steps.append(Step("токенов в отслеживаемых файлах нет",
                      not output.strip(), output.strip()[:120]))
    return steps


def main() -> int:
    groups = (
        ("Контракт жюри", check_contract),
        ("Точность", check_accuracy),
        ("Тесты", check_tests),
        ("Сторож фактов", check_consistency),
        ("Секреты", check_no_secrets),
    )

    print("Проверка решения ТЗ 5\n")
    failed = 0
    for title, probe in groups:
        print(title)
        try:
            steps = probe()
        except Exception as error:                  # noqa: BLE001
            steps = [Step(title, False, f"проверка упала: {error}")]
        for step in steps:
            print(step.line())
            failed += not step.ok
        print()

    if failed:
        print(f"ИТОГ: {failed} проверок не прошло. Коммитить нельзя.")
        return 1
    print("ИТОГ: всё зелёное.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
