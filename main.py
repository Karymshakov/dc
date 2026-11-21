#!/usr/bin/env python3
"""Единая точка входа: `eval`, `ask`, `bot`.

    python main.py ask "Как проверить баланс?"      ответ в JSON
    python main.py ask "А сколько стоит?" --chat 7  тот же чат, с контекстом
    python main.py ask "роуминг" --card             как это выглядит в боте
    python main.py eval --split tune --errors       точность по набору
    python main.py bot                              Telegram + CRM операторов
    python main.py crm                              только CRM, без Telegram

Команды `ask` и `eval` работают без токена и без `python-telegram-bot`:
функция `answer()` ядро проекта, а бот — надстройка над ней.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _use_utf8() -> None:
    """Включить UTF-8 в консоли: иначе кыргызские буквы ломают вывод."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):   # pragma: no cover
                pass


def command_ask(args: argparse.Namespace) -> int:
    """Напечатать ответ `answer()` в JSON, по желанию — карточку бота."""
    from faq_bot.answer import respond

    reply = respond(args.text, chat_id=args.chat)
    print(json.dumps(reply.to_dict(), ensure_ascii=False))

    if args.card:
        from faq_bot import presentation

        screen = presentation.answer_card(reply)
        print()
        print(presentation.plain_text(screen))
        print()
        for row in screen.rows:
            print("[" + "] [".join(button.label for button in row) + "]")
    return 0


def command_eval(rest: list[str]) -> int:
    """Прогнать измерительную оснастку из tools/evaluate.py."""
    from tools.evaluate import main as evaluate_main

    return int(evaluate_main(rest) or 0)


def command_bot(args: argparse.Namespace) -> int:
    """Запустить Telegram-бота. Без токена — понятное сообщение."""
    from faq_bot.bot import run

    return run(args.token, crm=not args.no_crm)


def command_crm(args: argparse.Namespace) -> int:
    """Веб-интерфейс операторов без Telegram: история, обучение, аналитика."""
    from faq_bot.crm.server import serve

    return serve(args.host, args.port)


def build_parser() -> argparse.ArgumentParser:
    """Разбор аргументов командной строки."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Бот поддержки INAI Mobile: ответы на вопросы на ru и ky",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    ask = commands.add_parser("ask", help="ответ на один вопрос в JSON")
    ask.add_argument("text", help="текст вопроса")
    ask.add_argument("--chat", default=None,
                     help="chat_id: включает контекст диалога")
    ask.add_argument("--card", action="store_true",
                     help="показать карточку ответа так, как её видит абонент")
    ask.set_defaults(handler=command_ask)

    evaluate = commands.add_parser(
        "eval", help="точность по размеченному набору",
        description="Аргументы передаются в tools/evaluate.py как есть: "
                    "--split tune|holdout|dev, --file, --errors",
    )
    evaluate.add_argument("rest", nargs="*", help="аргументы tools/evaluate.py")
    evaluate.set_defaults(handler=lambda args: command_eval(args.rest))

    bot = commands.add_parser("bot", help="запустить Telegram-бота")
    bot.add_argument("--token", default=None,
                     help="токен; по умолчанию из TELEGRAM_BOT_TOKEN")
    bot.add_argument("--no-crm", action="store_true",
                     help="не поднимать веб-интерфейс операторов")
    bot.set_defaults(handler=command_bot)

    crm = commands.add_parser("crm", help="веб-интерфейс операторов без Telegram")
    crm.add_argument("--host", default=None, help="по умолчанию 127.0.0.1")
    crm.add_argument("--port", type=int, default=None, help="по умолчанию 8765")
    crm.set_defaults(handler=command_crm)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа."""
    _use_utf8()
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Аргументы eval отдаём инструменту целиком: argparse иначе пытается
    # разобрать его «--split» сам и падает на незнакомом ключе.
    wants_help = "-h" in arguments[1:] or "--help" in arguments[1:]
    if arguments and arguments[0] == "eval" and not wants_help:
        return command_eval(arguments[1:])
    args = build_parser().parse_args(arguments)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
