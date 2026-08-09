#!/usr/bin/env python3
"""Поиск и загрузка страниц через Bright Data (SERP API + Web Unlocker).

Зачем: обычный WebFetch ходит с датацентрового IP и на части афиш и
агрегаторов получает антибот-заглушку или пустую страницу. Bright Data
отдаёт то, что видит живой браузер.

Почему именно REST API, а не прокси: весь исходящий трафик сессии обязан
идти через агентский прокси окружения, а тот не умеет нестандартные
HTTPS-порты — поэтому brd.superproxy.io:22225 отсюда недоступен в принципе.
Unified API живёт на api.brightdata.com:443 и проходит штатно; домен должен
быть в allowlist окружения.

Переменные окружения:
    BRIGHTDATA_API_KEY        — ключ API (обязательно)
    BRIGHTDATA_SERP_ZONE      — зона SERP API (по умолчанию serp)
    BRIGHTDATA_UNLOCKER_ZONE  — зона Web Unlocker (по умолчанию web_unlocker)

Примеры:

    python3 tools/brightdata.py search "moscow language exchange расписание"
    python3 tools/brightdata.py search "AI митап Москва" --engine yandex --limit 5
    python3 tools/brightdata.py fetch https://kudago.com/msk/ --limit-chars 20000
    python3 tools/brightdata.py fetch https://example.com/event --raw
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

API_URL = 'https://api.brightdata.com/request'

DEFAULT_SERP_ZONE = 'serp'
DEFAULT_UNLOCKER_ZONE = 'web_unlocker'

# Теги, содержимое которых в текст не попадает.
SKIP_TAGS = {'script', 'style', 'noscript', 'template', 'svg', 'head'}
# Теги, вокруг которых нужен перенос строки, иначе текст слипается в кашу.
BLOCK_TAGS = {
    'p', 'div', 'br', 'li', 'tr', 'section', 'article', 'header', 'footer',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'table', 'blockquote',
}


class TextExtractor(HTMLParser):
    """Вытаскивает читаемый текст из HTML — без зависимостей."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        elif tag in BLOCK_TAGS:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif tag in BLOCK_TAGS:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip_depth and data.strip():
            self.parts.append(data)

    def text(self):
        joined = ''.join(self.parts)
        joined = re.sub(r'[ \t\xa0]+', ' ', joined)
        joined = re.sub(r' *\n *', '\n', joined)
        return re.sub(r'\n{3,}', '\n\n', joined).strip()


def html_to_text(markup):
    parser = TextExtractor()
    try:
        parser.feed(markup)
    except Exception:
        # Битую разметку добираем тем, что успело распарситься.
        pass
    return parser.text()


def api_key():
    key = os.environ.get('BRIGHTDATA_API_KEY')
    if not key:
        sys.exit('Не задан $BRIGHTDATA_API_KEY — положите ключ в переменные окружения.')
    return key


def call(zone, target_url, attempts=3, timeout=120):
    """Один запрос к unified API Bright Data. Возвращает тело ответа строкой."""
    payload = json.dumps({'zone': zone, 'url': target_url, 'format': 'raw'}).encode('utf-8')
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            'Authorization': f'Bearer {api_key()}',
            'Content-Type': 'application/json',
        },
    )
    delay = 3
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace')[:500]
            if e.code in (401, 403) and 'brightdata' not in body.lower():
                hint = ''
                if os.environ.get('HTTPS_PROXY'):
                    hint = ('\nЕсли это 403 от агентского прокси, добавьте api.brightdata.com '
                            'в allowlist окружения (Network access -> Custom).')
                raise SystemExit(f'Bright Data вернул {e.code}: {body}{hint}')
            # 4xx кроме лимитов — ошибка запроса, ретраи не помогут.
            if 400 <= e.code < 500 and e.code != 429:
                raise SystemExit(f'Bright Data вернул {e.code}: {body}')
            last_error = f'{e.code}: {body}'
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = str(e)
        if attempt < attempts:
            time.sleep(delay)
            delay *= 2
    raise SystemExit(f'Bright Data недоступен после {attempts} попыток: {last_error}')


def search_url(engine, query, lang, country):
    q = urllib.parse.quote_plus(query)
    if engine == 'google':
        return f'https://www.google.com/search?q={q}&hl={lang}&gl={country}&brd_json=1'
    if engine == 'bing':
        return f'https://www.bing.com/search?q={q}&setlang={lang}&brd_json=1'
    if engine == 'yandex':
        return f'https://yandex.ru/search/?text={q}'
    sys.exit(f'Неизвестный движок: {engine}')


def parse_results(body, limit):
    """SERP API с brd_json=1 отдаёт JSON; иначе разбираем как текст."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    organic = data.get('organic') or data.get('results') or []
    rows = []
    for item in organic[:limit]:
        rows.append({
            'title': item.get('title') or '',
            'url': item.get('link') or item.get('url') or '',
            'snippet': item.get('description') or item.get('snippet') or '',
        })
    return rows


def cmd_search(args):
    zone = os.environ.get('BRIGHTDATA_SERP_ZONE', DEFAULT_SERP_ZONE)
    body = call(zone, search_url(args.engine, args.query, args.lang, args.country))
    results = parse_results(body, args.limit)

    if results is None:
        # Движок отдал HTML вместо JSON — показываем текст, чтобы запрос не пропал зря.
        print(html_to_text(body)[:args.limit_chars])
        return
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    if not results:
        print('Ничего не найдено.')
        return
    for i, row in enumerate(results, 1):
        print(f'{i}. {row["title"]}')
        print(f'   {row["url"]}')
        if row['snippet']:
            print(f'   {row["snippet"]}')
        print()


def cmd_fetch(args):
    zone = os.environ.get('BRIGHTDATA_UNLOCKER_ZONE', DEFAULT_UNLOCKER_ZONE)
    body = call(zone, args.url)
    out = body if args.raw else html_to_text(body)
    if args.limit_chars and len(out) > args.limit_chars:
        out = out[:args.limit_chars] + f'\n\n[...обрезано, всего {len(body)} символов]'
    print(out)


def main():
    parser = argparse.ArgumentParser(description='Bright Data: поиск и загрузка страниц.')
    sub = parser.add_subparsers(dest='command', required=True)

    s = sub.add_parser('search', help='Поиск через SERP API.')
    s.add_argument('query')
    s.add_argument('--engine', default='google', choices=['google', 'bing', 'yandex'])
    s.add_argument('--limit', type=int, default=10, help='Сколько результатов показать.')
    s.add_argument('--lang', default='ru')
    s.add_argument('--country', default='ru')
    s.add_argument('--json', action='store_true', help='Выдать JSON вместо текста.')
    s.add_argument('--limit-chars', type=int, default=20000)
    s.set_defaults(func=cmd_search)

    f = sub.add_parser('fetch', help='Загрузка страницы через Web Unlocker.')
    f.add_argument('url')
    f.add_argument('--raw', action='store_true', help='Отдать HTML как есть, без вычистки.')
    f.add_argument('--limit-chars', type=int, default=40000,
                   help='Обрезать вывод, чтобы не раздувать контекст (0 — не обрезать).')
    f.set_defaults(func=cmd_fetch)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
