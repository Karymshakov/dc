#!/usr/bin/env python3
"""Запуск бота ровно в одном экземпляре.

Поводом стал живой тест: одновременно работали три процесса, запущенные
в разное время. Они дрались за очередь Telegram, отвечали на одно
сообщение по нескольку раз и выполняли разные версии кода — выглядело
как хаос в интерфейсе, хотя интерфейс был ни при чём.

    python tools/run_bot.py            остановить старые и запустить новый
    python tools/run_bot.py --stop     только остановить
    python tools/run_bot.py --status   кто сейчас работает

Telegram сам сообщает о такой ситуации ошибкой Conflict, но замечаешь её
только в логе, который никто не читает. Проще не допускать.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MARKER = "main.py"          # по чему узнаём свой процесс
LOG = PROJECT_ROOT / "bot.log"


def load_env() -> dict[str, str]:
    """Переменные из .env. Файл в репозиторий не попадает."""
    values: dict[str, str] = {}
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


def running_bots() -> list[tuple[int, str]]:
    """Процессы, похожие на наш бот: (pid, время запуска)."""
    if os.name != "nt":
        found = subprocess.run(["ps", "-eo", "pid,lstart,args"],
                               capture_output=True, text=True)
        result = []
        for line in found.stdout.splitlines():
            if MARKER in line and " bot" in line and "run_bot" not in line:
                parts = line.split(None, 1)
                result.append((int(parts[0]), parts[1][:24]))
        return result

    # Отбор идёт в Python, а не внутри PowerShell: путь к проекту содержит
    # кириллицу, консоль Windows отдаёт её в системной кодировке, и фильтр
    # по строке командной строки внутри PowerShell на этом ломается.
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match 'python' } | "
        "ForEach-Object { \"$($_.ProcessId)|"
        "$($_.CreationDate.ToString('HH:mm:ss'))|$($_.CommandLine)\" }"
    )
    found = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                           capture_output=True)
    stdout = found.stdout.decode("utf-8", errors="replace")

    result: list[tuple[int, str]] = []
    for line in stdout.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) != 3:
            continue
        pid_text, started, command = parts
        # Имя файла и подкоманда латиницей — порча кириллицы в пути не мешает.
        if MARKER not in command or " bot" not in command:
            continue
        if "run_bot" in command:
            continue
        try:
            result.append((int(pid_text), started))
        except ValueError:
            continue
    return result


def stop_all() -> int:
    """Остановить все найденные экземпляры. Возвращает сколько остановлено."""
    bots = running_bots()
    for pid, started in bots:
        print(f"  останавливаю PID {pid} (запущен {started})")
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True)
        else:
            subprocess.run(["kill", "-9", str(pid)], capture_output=True)
    if bots:
        time.sleep(2)
    return len(bots)


def start() -> int:
    environment = os.environ.copy()
    environment.update(load_env())

    if not environment.get("TELEGRAM_BOT_TOKEN"):
        print("Нет TELEGRAM_BOT_TOKEN: положите его в .env или в переменные окружения.")
        return 2

    handle = LOG.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "main.py"), "bot"],
        cwd=PROJECT_ROOT, stdout=handle, stderr=subprocess.STDOUT,
        env=environment,
    )

    # Даём боту подключиться и убеждаемся, что он не упал сразу.
    time.sleep(8)
    if process.poll() is not None:
        print(f"Бот упал. Последние строки {LOG.name}:")
        print(LOG.read_text(encoding="utf-8", errors="replace")[-600:])
        return 1

    print(f"  запущен PID {process.pid}, лог: {LOG.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Запуск бота в одном экземпляре")
    parser.add_argument("--stop", action="store_true", help="только остановить")
    parser.add_argument("--status", action="store_true", help="кто работает")
    args = parser.parse_args(argv)

    if args.status:
        bots = running_bots()
        if not bots:
            print("Бот не запущен.")
        for pid, started in bots:
            print(f"  PID {pid}, запущен {started}")
        if len(bots) > 1:
            print("\nЭкземпляров больше одного — они будут мешать друг другу.")
            print("Исправить: python tools/run_bot.py")
        return 0

    stopped = stop_all()
    print(f"остановлено старых: {stopped}")
    if args.stop:
        return 0

    code = start()
    if code:
        return code

    remaining = running_bots()
    if len(remaining) != 1:
        print(f"ВНИМАНИЕ: экземпляров {len(remaining)}, ожидался один")
        return 1
    print("\nБот работает в одном экземпляре.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
