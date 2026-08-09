# Еженедельный отчёт в Telegram

Расписание живёт **не в этом репозитории**: отчёт раз в неделю собирает
отложенная задача Claude Code (Routine), которая запускается по воскресеньям
утром, ищет события на неделю вперёд по брифу
[`weekly_report_prompt.md`](weekly_report_prompt.md), складывает результат в
файл и вызывает скрипты отсюда. Код в репозитории отвечает только за рендер и
отправку готового отчёта.

Поиск событий идёт через MCP-коннектор Bright Data (`mcp__bd__*`) — он
подключён к самой задаче, отдельного кода и сетевых разрешений не требует:
трафик коннектора идёт через серверы Anthropic, а не через сеть сессии.

В чат уходит PDF, а не текст:

* [`render_report_pdf.py`](render_report_pdf.py) — markdown в PDF через
  headless Chromium, который уже стоит в окружении. Кириллица берётся из
  DejaVu Sans, внешних библиотек не нужно.
* [`send_telegram_report.py`](send_telegram_report.py) — отправка. С `--as-pdf`
  сам рендерит и шлёт документом, подпись берёт из заголовка отчёта.

## Переменные окружения: по паре на каждого бота

Ботов может быть несколько, поэтому переменные именованы по боту. Для бота
`NAME` скрипт читает `TELEGRAM_<NAME>_BOT_TOKEN` и `TELEGRAM_<NAME>_CHAT_ID`.
Какой бот использовать — задаёт `--bot` (по умолчанию `weekly_report`) или
переменная `TELEGRAM_BOT`.

Еженедельному отчёту нужны:

```
TELEGRAM_WEEKLY_REPORT_BOT_TOKEN=...
TELEGRAM_WEEKLY_REPORT_CHAT_ID=...
```

Следующий бот добавляется рядом, ничего не переопределяя:

```
TELEGRAM_ALERTS_BOT_TOKEN=...
TELEGRAM_ALERTS_CHAT_ID=...
```

и вызывается как `--bot alerts`. Общих `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
намеренно нет: с несколькими ботами такой фолбэк однажды отправит сообщение не
туда. `--list-bots` покажет, что настроено в текущем окружении.

`TELEGRAM_<NAME>_CHAT_ID` можно не задавать — тогда скрипт возьмёт чат из
последних апдейтов бота, достаточно один раз ему написать.

Переменные задаются в настройках окружения на claude.ai/code (иконка облака →
шестерёнка у окружения → поле Environment variables, формат `.env`). Учтите,
что выделенного хранилища секретов там нет и значения видны всем, кто
пользуется окружением, — заводите отдельного бота под каждую задачу, чтобы
токен можно было отозвать по отдельности.

Домен `api.telegram.org` должен быть разрешён в network policy окружения: при
уровне Trusted его в списке нет, нужен уровень Custom с этим доменом и
включённой галочкой про список по умолчанию.

> В `tga/tga/settings.py` лежит токен из исходного туториала, закоммиченный в
> историю репозитория. Скрипт его не использует и использовать не стоит.

## Использование

```bash
# какие боты настроены в окружении
python3 tools/send_telegram_report.py --list-bots

# проверить связь и найти свой chat id
python3 tools/send_telegram_report.py --whoami
python3 tools/send_telegram_report.py --whoami --bot alerts

# отправить отчёт PDF-файлом (основной режим)
python3 tools/send_telegram_report.py report.md --as-pdf

# отправить уже готовый файл
python3 tools/send_telegram_report.py --document /tmp/report.pdf --caption "План недели"

# собрать PDF, ничего не отправляя
python3 tools/render_report_pdf.py report.md -o /tmp/report.pdf
python3 tools/render_report_pdf.py report.md --html-only   # посмотреть вёрстку

# текстом, как раньше: посмотреть разбивку и отправить
python3 tools/send_telegram_report.py report.md --dry-run
python3 tools/send_telegram_report.py report.md
python3 tools/send_telegram_report.py report.md --bot alerts
cat report.md | python3 tools/send_telegram_report.py -
```

Скрипт на стандартной библиотеке, зависимостей ставить не нужно. Он переводит
markdown в поддерживаемое Telegram подмножество HTML, режет текст по границам
абзацев на сообщения до 4096 символов, повторяет запрос при сетевых сбоях и
ждёт `retry_after` при 429.

## Как поменять содержание отчёта

Правьте [`weekly_report_prompt.md`](weekly_report_prompt.md) — задача читает
этот файл при каждом запуске. Расписание и сам факт запуска меняются в
Routine (`/routines` или настройки задач в Claude Code), а не в коде.
