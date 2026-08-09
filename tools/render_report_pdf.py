#!/usr/bin/env python3
"""Рендер markdown-отчёта в PDF через headless Chromium.

Chromium уже стоит в окружении, а DejaVu Sans покрывает кириллицу, поэтому
внешние зависимости не нужны: markdown переводится в HTML, HTML печатается
в PDF.

Примеры:

    python3 tools/render_report_pdf.py report.md
    python3 tools/render_report_pdf.py report.md -o /tmp/otchet.pdf
    python3 tools/render_report_pdf.py report.md --html-only  # посмотреть вёрстку
"""

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile

CHROMIUM_CANDIDATES = [
    os.environ.get('CHROMIUM_PATH'),
    '/opt/pw-browsers/chromium',
    '/opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell',
    'chromium',
    'chromium-browser',
    'google-chrome',
]

STYLE = """
@page { size: A4; margin: 16mm 14mm 18mm 14mm; }
* { box-sizing: border-box; }
body {
  font-family: "DejaVu Sans", "Liberation Sans", sans-serif;
  font-size: 10.5pt; line-height: 1.5; color: #1a1a1a; margin: 0;
}
h1 {
  font-size: 19pt; line-height: 1.25; margin: 0 0 4mm; color: #0f172a;
  border-bottom: 2px solid #0f172a; padding-bottom: 3mm;
}
h2 {
  font-size: 13pt; margin: 8mm 0 3mm; color: #0f172a;
  border-top: 1px solid #cbd5e1; padding-top: 3mm; page-break-after: avoid;
}
h3 {
  font-size: 11.5pt; margin: 6mm 0 2mm; color: #1e3a5f; page-break-after: avoid;
}
h4, h5, h6 { font-size: 10.5pt; margin: 4mm 0 2mm; page-break-after: avoid; }
p { margin: 0 0 2.5mm; }
ul, ol { margin: 0 0 3mm; padding-left: 6mm; }
li { margin: 0 0 1.5mm; }
li > ul, li > ol { margin-top: 1.5mm; }
strong { color: #0f172a; }
a { color: #1d4ed8; text-decoration: none; word-break: break-word; }
code {
  font-family: "DejaVu Sans Mono", monospace; font-size: 9pt;
  background: #f1f5f9; padding: 0.4mm 1mm; border-radius: 1mm;
}
blockquote {
  margin: 0 0 3mm; padding: 2mm 0 2mm 4mm;
  border-left: 3px solid #cbd5e1; color: #475569;
}
hr { border: 0; border-top: 1px solid #cbd5e1; margin: 5mm 0; }
/* Пункт дня не должен разрываться между страницами посередине. */
li, blockquote { page-break-inside: avoid; }
"""

INLINE_CODE = re.compile(r'`([^`]+)`')
LINK = re.compile(r'\[([^\]]+)\]\((https?://[^)\s]+|t\.me/[^)\s]+|[^)\s]+)\)')
BOLD = re.compile(r'\*\*([^*]+)\*\*')
ITALIC = re.compile(r'(?<![*\w])\*([^*\n]+)\*(?!\*)')
HEADING = re.compile(r'^(#{1,6})\s+(.*)$')
BULLET = re.compile(r'^(\s*)[-*]\s+(.*)$')
NUMBERED = re.compile(r'^(\s*)(\d+)[.)]\s+(.*)$')


def inline(text):
    """Инлайновая разметка. Готовые куски прячем, остальное экранируем."""
    stash = []

    def keep(fragment):
        stash.append(fragment)
        return f'\x00{len(stash) - 1}\x00'

    text = INLINE_CODE.sub(lambda m: keep(f'<code>{html.escape(m.group(1))}</code>'), text)
    text = LINK.sub(
        lambda m: keep(
            f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>'
        ),
        text,
    )
    text = BOLD.sub(lambda m: keep(f'<strong>{html.escape(m.group(1))}</strong>'), text)
    text = ITALIC.sub(lambda m: keep(f'<em>{html.escape(m.group(1))}</em>'), text)
    text = html.escape(text)
    return re.sub(r'\x00(\d+)\x00', lambda m: stash[int(m.group(1))], text)


def markdown_to_html(markdown):
    """Подмножество markdown, которого хватает отчёту."""
    out = []
    list_stack = []  # открытые списки: 'ul' или 'ol'
    paragraph = []

    def close_paragraph():
        if paragraph:
            out.append(f'<p>{inline(" ".join(paragraph))}</p>')
            paragraph.clear()

    def close_lists(depth=0):
        while len(list_stack) > depth:
            out.append(f'</{list_stack.pop()}>')

    for raw in markdown.split('\n'):
        line = raw.rstrip()

        if not line.strip():
            close_paragraph()
            close_lists()
            continue

        heading = HEADING.match(line)
        if heading:
            close_paragraph()
            close_lists()
            level = len(heading.group(1))
            out.append(f'<h{level}>{inline(heading.group(2))}</h{level}>')
            continue

        if re.fullmatch(r'\s*([-*_]\s*){3,}', line):
            close_paragraph()
            close_lists()
            out.append('<hr>')
            continue

        if line.lstrip().startswith('>'):
            close_paragraph()
            close_lists()
            out.append(f'<blockquote>{inline(line.lstrip()[1:].strip())}</blockquote>')
            continue

        bullet = BULLET.match(line)
        numbered = NUMBERED.match(line)
        if bullet or numbered:
            close_paragraph()
            match = bullet or numbered
            indent = len(match.group(1).replace('\t', '  '))
            kind = 'ul' if bullet else 'ol'
            # Любой отступ означает вложенность: в отчёте подпункты дня идут
            # с одним пробелом под нумерованным пунктом события.
            depth = 1 if indent == 0 else 2 + (indent - 1) // 2
            close_lists(depth)
            # На той же глубине маркер мог смениться с "-" на "1." и обратно.
            if len(list_stack) == depth and list_stack[-1] != kind:
                close_lists(depth - 1)
            while len(list_stack) < depth:
                out.append(f'<{kind}>')
                list_stack.append(kind)
            content = bullet.group(2) if bullet else numbered.group(3)
            out.append(f'<li>{inline(content)}</li>')
            continue

        if list_stack:
            # Продолжение пункта списка со следующей строки — дописываем в тот же <li>.
            if out and out[-1].endswith('</li>'):
                out[-1] = f'{out[-1][:-len("</li>")]} {inline(line.strip())}</li>'
            else:
                out.append(f'<li>{inline(line.strip())}</li>')
            continue

        paragraph.append(line.strip())

    close_paragraph()
    close_lists()

    title = 'Отчёт'
    first = HEADING.match(markdown.strip().split('\n')[0] if markdown.strip() else '')
    if first:
        title = re.sub(r'[*`]', '', first.group(2))[:120]

    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        f'<title>{html.escape(title)}</title><style>{STYLE}</style>'
        f'</head><body>{"".join(out)}</body></html>'
    )


def find_chromium():
    for candidate in CHROMIUM_CANDIDATES:
        if not candidate:
            continue
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    sys.exit('Не найден Chromium. Укажите путь в $CHROMIUM_PATH.')


def render_pdf(html_text, pdf_path):
    chromium = find_chromium()
    with tempfile.TemporaryDirectory() as workdir:
        html_path = os.path.join(workdir, 'report.html')
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_text)
        command = [
            chromium,
            '--headless',
            '--disable-gpu',
            '--no-sandbox',
            '--no-pdf-header-footer',
            f'--user-data-dir={os.path.join(workdir, "profile")}',
            '--virtual-time-budget=5000',
            f'--print-to-pdf={pdf_path}',
            f'file://{html_path}',
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
        sys.exit(f'Chromium не создал PDF (код {result.returncode}):\n{result.stderr[-1500:]}')
    return pdf_path


def main():
    parser = argparse.ArgumentParser(description='Markdown-отчёт в PDF через Chromium.')
    parser.add_argument('path', help='Файл с markdown, "-" — читать stdin.')
    parser.add_argument('-o', '--output', help='Куда сохранить PDF (по умолчанию рядом, .pdf).')
    parser.add_argument('--html-only', action='store_true', help='Вывести HTML и выйти.')
    args = parser.parse_args()

    markdown = sys.stdin.read() if args.path == '-' else open(args.path, encoding='utf-8').read()
    markdown = markdown.strip()
    if not markdown:
        sys.exit('Пустой отчёт — рендерить нечего.')

    document = markdown_to_html(markdown)
    if args.html_only:
        print(document)
        return

    output = args.output
    if not output:
        base = 'report' if args.path == '-' else os.path.splitext(args.path)[0]
        output = f'{base}.pdf'
    render_pdf(document, os.path.abspath(output))
    size = os.path.getsize(output)
    print(f'PDF готов: {output} ({size // 1024} КБ)')


if __name__ == '__main__':
    main()
