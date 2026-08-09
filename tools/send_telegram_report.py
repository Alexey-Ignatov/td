#!/usr/bin/env python3
"""Отправка markdown-отчёта в Telegram через Bot API.

Зависимостей нет — только стандартная библиотека. Прокси и CA-бандл берутся
из окружения (https_proxy / SSL_CERT_FILE), как это делает urllib по умолчанию.

Примеры:

    python3 tools/send_telegram_report.py report.md
    cat report.md | python3 tools/send_telegram_report.py -
    python3 tools/send_telegram_report.py report.md --chat-id 123456789

Токен ищется в таком порядке: --token, $TELEGRAM_BOT_TOKEN, TOKEN из
tga/tga/settings.py. Chat id: --chat-id, $TELEGRAM_CHAT_ID.
Узнать свой chat id можно, написав боту любое сообщение — он отвечает
«Ваш ID = ...» (см. ugc/management/commands/bot.py), либо запустив этот
скрипт с флагом --whoami.
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = 'https://api.telegram.org'

# Telegram режет сообщения на 4096 символах, оставляем запас на служебный префикс.
CHUNK_LIMIT = 3800

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(REPO_ROOT, 'tga', 'tga', 'settings.py')


def resolve_token(explicit):
    if explicit:
        return explicit
    env = os.environ.get('TELEGRAM_BOT_TOKEN')
    if env:
        return env
    try:
        with open(SETTINGS_PATH, encoding='utf-8') as f:
            m = re.search(r'^TOKEN\s*=\s*[\'"]([^\'"]+)[\'"]', f.read(), re.M)
    except OSError:
        m = None
    if m:
        return m.group(1)
    sys.exit('Не найден токен бота: задайте $TELEGRAM_BOT_TOKEN или передайте --token.')


def resolve_chat_id(explicit):
    chat_id = explicit or os.environ.get('TELEGRAM_CHAT_ID')
    if not chat_id:
        sys.exit('Не найден chat id: задайте $TELEGRAM_CHAT_ID или передайте --chat-id.')
    return chat_id


def api_call(token, method, payload=None, attempts=4):
    """Вызов Bot API с ретраями и уважением к 429 retry_after."""
    url = f'{API}/bot{token}/{method}'
    data = urllib.parse.urlencode(payload or {}).encode('utf-8')
    delay = 2
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, data=data if payload else None)
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace')
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = {}
            if e.code == 429:
                wait = parsed.get('parameters', {}).get('retry_after', delay)
                time.sleep(wait)
                last_error = f'429: {body}'
                continue
            # 4xx кроме 429 — это ошибка запроса, ретраи не помогут.
            if 400 <= e.code < 500:
                raise SystemExit(f'Telegram API {method} вернул {e.code}: {body}')
            last_error = f'{e.code}: {body}'
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = str(e)
        if attempt < attempts:
            time.sleep(delay)
            delay *= 2
    raise SystemExit(f'Не удалось вызвать {method} после {attempts} попыток: {last_error}')


def markdown_to_html(text):
    """Подмножество markdown -> HTML, который понимает Telegram.

    Telegram-разметка поддерживает лишь несколько тегов, поэтому всё остальное
    экранируется. MarkdownV2 не используем: он требует экранировать почти
    каждый знак препинания и ломается на живых текстах отчёта.
    """
    out = []
    for line in text.split('\n'):
        heading = re.match(r'^(#{1,6})\s+(.*)$', line)
        if heading:
            out.append(f'<b>{inline_to_html(heading.group(2))}</b>')
            continue
        bullet = re.match(r'^(\s*)[-*]\s+(.*)$', line)
        if bullet:
            out.append(f'{bullet.group(1)}• {inline_to_html(bullet.group(2))}')
            continue
        out.append(inline_to_html(line))
    return '\n'.join(out)


def inline_to_html(text):
    placeholders = []

    def stash(tag_html):
        placeholders.append(tag_html)
        return f'\x00{len(placeholders) - 1}\x00'

    text = re.sub(
        r'`([^`]+)`',
        lambda m: stash(f'<code>{html.escape(m.group(1))}</code>'),
        text,
    )
    text = re.sub(
        r'\[([^\]]+)\]\((https?://[^)\s]+)\)',
        lambda m: stash(
            f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>'
        ),
        text,
    )
    text = re.sub(
        r'\*\*([^*]+)\*\*',
        lambda m: stash(f'<b>{html.escape(m.group(1))}</b>'),
        text,
    )
    text = html.escape(text)
    return re.sub(r'\x00(\d+)\x00', lambda m: placeholders[int(m.group(1))], text)


def split_chunks(text, limit=CHUNK_LIMIT):
    """Режет текст по границам абзацев/строк, не разрывая теги внутри строки."""
    chunks = []
    current = ''
    for block in text.split('\n\n'):
        candidate = f'{current}\n\n{block}' if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ''
        if len(block) <= limit:
            current = block
            continue
        for line in block.split('\n'):
            candidate = f'{current}\n{line}' if current else line
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                # Строка длиннее лимита — режем жёстко, иначе она не уйдёт вовсе.
                while len(line) > limit:
                    chunks.append(line[:limit])
                    line = line[limit:]
                current = line
    if current:
        chunks.append(current)
    return chunks


def main():
    parser = argparse.ArgumentParser(description='Отправить markdown-отчёт в Telegram.')
    parser.add_argument('path', nargs='?', help='Файл с отчётом, "-" — читать stdin.')
    parser.add_argument('--token', help='Токен бота (по умолчанию $TELEGRAM_BOT_TOKEN).')
    parser.add_argument('--chat-id', help='Chat id получателя (по умолчанию $TELEGRAM_CHAT_ID).')
    parser.add_argument('--plain', action='store_true', help='Слать как есть, без разметки.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Показать разбивку на сообщения и не отправлять.')
    parser.add_argument('--whoami', action='store_true',
                        help='Показать getMe и chat id из последних апдейтов и выйти.')
    args = parser.parse_args()

    if args.dry_run:
        if not args.path:
            parser.error('нужен путь к файлу отчёта (или "-" для stdin)')
        text = sys.stdin.read() if args.path == '-' else open(args.path, encoding='utf-8').read()
        body = text.strip() if args.plain else markdown_to_html(text.strip())
        chunks = split_chunks(body)
        for i, chunk in enumerate(chunks, 1):
            print(f'--- сообщение {i}/{len(chunks)} ({len(chunk)} символов) ---')
            print(chunk)
        return

    token = resolve_token(args.token)

    if args.whoami:
        me = api_call(token, 'getMe')
        print(json.dumps(me, ensure_ascii=False, indent=2))
        updates = api_call(token, 'getUpdates', {'limit': 10})
        seen = []
        for update in updates.get('result', []):
            chat = (update.get('message') or {}).get('chat') or {}
            if chat.get('id') and chat['id'] not in [c['id'] for c in seen]:
                seen.append({'id': chat['id'], 'name': chat.get('username') or chat.get('title')})
        print('Найденные chat id:', json.dumps(seen, ensure_ascii=False) or 'нет — напишите боту')
        return

    if not args.path:
        parser.error('нужен путь к файлу отчёта (или "-" для stdin)')

    chat_id = resolve_chat_id(args.chat_id)
    text = sys.stdin.read() if args.path == '-' else open(args.path, encoding='utf-8').read()
    text = text.strip()
    if not text:
        sys.exit('Отчёт пустой — отправлять нечего.')

    body = text if args.plain else markdown_to_html(text)
    chunks = split_chunks(body)
    for i, chunk in enumerate(chunks, 1):
        payload = {
            'chat_id': chat_id,
            'text': chunk,
            'disable_web_page_preview': 'true',
        }
        if not args.plain:
            payload['parse_mode'] = 'HTML'
        api_call(token, 'sendMessage', payload)
        print(f'Отправлено {i}/{len(chunks)} ({len(chunk)} символов)')
        if i < len(chunks):
            time.sleep(1)  # Telegram лимитирует частоту сообщений в один чат.


if __name__ == '__main__':
    main()
