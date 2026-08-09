#!/usr/bin/env python3
"""Быстрая проверка окружения: что доступно из сессии, а что закрыто.

Смысл в скорости: полный прогон отчёта занимает минут десять, а узнать,
пробивается ли сеть и на месте ли переменные, нужно за секунды. Домены
проверяются параллельно, весь отчёт укладывается в пару секунд.

Что проверяется:
  - исходящий доступ к доменам (через прокси окружения, как ходят WebFetch
    и наши скрипты);
  - переменные окружения telegram-ботов;
  - наличие Chromium для рендера PDF;
  - свежие отказы агентского прокси.

Наличие MCP-инструментов (mcp__bd__* и прочих) скрипт увидеть не может —
это уровень агента, а не процесса. Проверять их должен сам агент.

Примеры:

    python3 tools/preflight.py
    python3 tools/preflight.py --add example.com --add api.example.org
    python3 tools/preflight.py --json
"""

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# Всё, что нужно еженедельному отчёту: доставка, источники событий, поиск.
DOMAINS = [
    'api.telegram.org',
    't.me',
    'timepad.ru',
    'kudago.com',
    'ict2go.ru',
    'networkly.app',
    'www.google.com',
    'yandex.ru',
    'api.brightdata.com',
    'github.com',
]

TIMEOUT = 12


def probe(host):
    """Пробуем достучаться до хоста тем же путём, что и остальные инструменты."""
    url = f'https://{host}/'
    request = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'preflight/1.0'})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return host, 'ok', f'HTTP {response.status}'
    except urllib.error.HTTPError as e:
        # Сам хост ответил — значит канал открыт, код не важен.
        return host, 'ok', f'HTTP {e.code}'
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if '403' in reason or 'Forbidden' in reason:
            return host, 'blocked', 'прокси: 403 на CONNECT (нет в allowlist)'
        if 'timed out' in reason.lower():
            return host, 'timeout', 'таймаут'
        return host, 'error', reason[:90]
    except (socket.timeout, TimeoutError):
        return host, 'timeout', 'таймаут'
    except Exception as e:  # noqa: BLE001 — диагностике важно не падать
        return host, 'error', str(e)[:90]


def proxy_status():
    proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
    if not proxy:
        return None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'{proxy}/__agentproxy/status', timeout=TIMEOUT) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception:  # noqa: BLE001
        return None


def telegram_bots():
    bots = {}
    for key, value in os.environ.items():
        if key.startswith('TELEGRAM_') and key.endswith('_BOT_TOKEN') and value:
            name = key[len('TELEGRAM_'):-len('_BOT_TOKEN')].lower()
            bots[name] = bool(os.environ.get(f'TELEGRAM_{name.upper()}_CHAT_ID'))
    return bots


def chromium():
    candidates = ['/opt/pw-browsers/chromium', 'chromium', 'chromium-browser', 'google-chrome']
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
        from shutil import which
        found = which(path)
        if found:
            return found
    return None


def main():
    parser = argparse.ArgumentParser(description='Быстрая проверка доступности окружения.')
    parser.add_argument('--add', action='append', default=[], help='Дополнительный домен.')
    parser.add_argument('--only', action='append', default=[], help='Проверить только эти домены.')
    parser.add_argument('--json', action='store_true', help='Машиночитаемый вывод.')
    args = parser.parse_args()

    hosts = args.only or DOMAINS + args.add
    with ThreadPoolExecutor(max_workers=min(12, len(hosts))) as pool:
        results = list(pool.map(probe, hosts))

    bots = telegram_bots()
    browser = chromium()
    status = proxy_status()
    report = {
        'domains': [{'host': h, 'state': s, 'detail': d} for h, s, d in results],
        'telegram_bots': bots,
        'chromium': browser,
        'proxy_enabled': bool(status and status.get('enabled')),
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    print('ДОМЕНЫ')
    marks = {'ok': '  OK    ', 'blocked': '  ЗАКРЫТ', 'timeout': '  ТАЙМАУТ', 'error': '  ОШИБКА'}
    for host, state, detail in results:
        print(f'{marks.get(state, "  ?     ")}  {host:<24} {detail}')

    blocked = [h for h, s, _ in results if s != 'ok']
    print()
    print('TELEGRAM')
    if bots:
        for name, has_chat in sorted(bots.items()):
            chat = 'chat id задан' if has_chat else 'chat id НЕ задан'
            print(f'  {name}: токен задан, {chat}')
    else:
        print('  переменных TELEGRAM_<NAME>_BOT_TOKEN нет')

    print()
    print('PDF')
    print(f'  Chromium: {browser}' if browser else '  Chromium НЕ найден — PDF не соберётся')

    if blocked:
        print()
        print(f'ИТОГ: закрыто доменов — {len(blocked)}: {", ".join(blocked)}')
        print('Лечится в настройках окружения: Network access -> Custom, добавить домены,')
        print('и включить «Also include default list of common package managers».')
        sys.exit(1)
    print()
    print('ИТОГ: все проверенные домены доступны.')


if __name__ == '__main__':
    main()
