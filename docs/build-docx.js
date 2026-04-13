const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, HeadingLevel, BorderStyle, WidthType,
  ShadingType, PageNumber, PageBreak, LevelFormat, TabStopType, TabStopPosition
} = require("docx");

const C = {
  accent: "1A73E8", accentDark: "0D47A1", accentLight: "E3F2FD",
  bg: "F5F7FA", border: "E1E4E8", text: "24292F", text2: "57606A",
  green: "1A7F37", greenBg: "DAFBE1", white: "FFFFFF",
};

const PAGE_W = 12240, MARGIN = 1440, CW = PAGE_W - MARGIN * 2;
const thin = { style: BorderStyle.SINGLE, size: 1, color: C.border };
const borders = { top: thin, bottom: thin, left: thin, right: thin };
const cm = { top: 60, bottom: 60, left: 120, right: 120 };

function h1(t) { return new Paragraph({ heading: HeadingLevel.HEADING_1, spacing: { before: 400, after: 200 }, children: [new TextRun({ text: t, bold: true })] }); }
function h2(t) { return new Paragraph({ heading: HeadingLevel.HEADING_2, spacing: { before: 300, after: 160 }, children: [new TextRun({ text: t, bold: true })] }); }
function p(t, opts = {}) { return new Paragraph({ spacing: { after: 140, ...(opts.spacing||{}) }, children: [new TextRun({ text: t, size: 22, ...(opts.run||{}) })] }); }
function bp(label, value) { return new Paragraph({ spacing: { after: 100 }, children: [new TextRun({ text: label, bold: true, size: 22 }), new TextRun({ text: value, size: 22 })] }); }
function li(t, ref = "b", lvl = 0) { return new Paragraph({ numbering: { reference: ref, level: lvl }, spacing: { after: 60 }, children: [new TextRun({ text: t, size: 22 })] }); }
function ni(t, ref = "n") { return new Paragraph({ numbering: { reference: ref, level: 0 }, spacing: { after: 60 }, children: [new TextRun({ text: t, size: 22 })] }); }
function hc(t, w) { return new TableCell({ borders, width: { size: w, type: WidthType.DXA }, shading: { fill: C.accentDark, type: ShadingType.CLEAR }, margins: cm, children: [new Paragraph({ children: [new TextRun({ text: t, bold: true, color: C.white, size: 20, font: "Arial" })] })] }); }
function tc(t, w, bg) { return new TableCell({ borders, width: { size: w, type: WidthType.DXA }, shading: bg ? { fill: bg, type: ShadingType.CLEAR } : undefined, margins: cm, children: [new Paragraph({ children: [new TextRun({ text: t, size: 20, font: "Arial" })] })] }); }
function tbl(hdrs, rows, cw) {
  const tw = cw.reduce((a,b) => a+b, 0);
  return new Table({ width: { size: tw, type: WidthType.DXA }, columnWidths: cw, rows: [
    new TableRow({ children: hdrs.map((h,i) => hc(h, cw[i])) }),
    ...rows.map((r,ri) => new TableRow({ children: r.map((c,ci) => tc(c, cw[ci], ri%2===0 ? C.bg : undefined)) }))
  ]});
}
function statBox(val, label) {
  return new TableCell({
    borders: { top: { style: BorderStyle.SINGLE, size: 3, color: C.accent }, bottom: thin, left: thin, right: thin },
    width: { size: CW/4, type: WidthType.DXA },
    shading: { fill: C.accentLight, type: ShadingType.CLEAR },
    margins: { top: 140, bottom: 140, left: 80, right: 80 },
    children: [
      new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: val, bold: true, size: 40, color: C.accentDark, font: "Arial" })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 40 }, children: [new TextRun({ text: label, size: 18, color: C.text2, font: "Arial" })] }),
    ],
  });
}
function quote(t) {
  return new Paragraph({
    spacing: { before: 120, after: 120 },
    shading: { fill: C.bg, type: ShadingType.CLEAR },
    border: { left: { style: BorderStyle.SINGLE, size: 6, color: C.accent, space: 8 } },
    indent: { left: 200 },
    children: [new TextRun({ text: t, size: 22, italics: true, color: C.text2 })]
  });
}

const doc = new Document({
  styles: {
    default: { document: { run: { font: "Arial", size: 22, color: C.text } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 36, bold: true, color: C.accentDark, font: "Arial" },
        paragraph: { spacing: { before: 400, after: 200 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 28, bold: true, color: C.accent, font: "Arial" },
        paragraph: { spacing: { before: 300, after: 160 }, outlineLevel: 1,
          border: { bottom: { style: BorderStyle.SINGLE, size: 2, color: C.accent, space: 4 } } } },
    ]
  },
  numbering: { config: [
    { reference: "b", levels: [
      { level: 0, format: LevelFormat.BULLET, text: "\u2022", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } },
      { level: 1, format: LevelFormat.BULLET, text: "\u25E6", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 1440, hanging: 360 } } } },
    ]},
    { reference: "n", levels: [
      { level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } },
    ]},
  ]},
  sections: [
    // ===== TITLE PAGE =====
    {
      properties: { page: { size: { width: PAGE_W, height: 15840 }, margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 } } },
      children: [
        new Paragraph({ spacing: { before: 2400 } }),
        new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 120 }, children: [
          new TextRun({ text: "memory-compiler", size: 60, bold: true, color: C.accentDark }),
        ]}),
        new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 80 }, children: [
          new TextRun({ text: "Персональная база знаний", size: 32, color: C.text2 }),
        ]}),
        new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 400 }, children: [
          new TextRun({ text: "для AI-ассистентов", size: 32, color: C.text2 }),
        ]}),
        new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 200 },
          border: { top: { style: BorderStyle.SINGLE, size: 2, color: C.accent, space: 12 } }, children: [] }),
        new Table({
          width: { size: CW, type: WidthType.DXA }, columnWidths: [CW/4, CW/4, CW/4, CW/4],
          rows: [new TableRow({ children: [
            statBox("16", "инструментов"),
            statBox("14", "API endpoints"),
            statBox("5", "вкладок UI"),
            statBox("27", "статей в базе"),
          ]})]
        }),
        new Paragraph({ spacing: { before: 600 }, alignment: AlignmentType.CENTER, children: [
          new TextRun({ text: "MCP-сервер на Docker | Гибридный поиск | Автокомпиляция | Web UI", size: 20, color: C.text2 }),
        ]}),
        new Paragraph({ spacing: { before: 1400 }, alignment: AlignmentType.CENTER, children: [
          new TextRun({ text: "Версия 4  |  Апрель 2026", size: 20, color: C.text2 }),
        ]}),
        new PageBreak(),
      ]
    },
    // ===== CONTENT =====
    {
      properties: { page: { size: { width: PAGE_W, height: 15840 }, margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 } } },
      headers: { default: new Header({ children: [new Paragraph({
        border: { bottom: { style: BorderStyle.SINGLE, size: 1, color: C.border, space: 4 } },
        children: [
          new TextRun({ text: "memory-compiler", bold: true, size: 18, color: C.accent }),
          new TextRun({ text: "\tОписание возможностей", size: 18, color: C.text2 }),
        ],
        tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
      })] }) },
      footers: { default: new Footer({ children: [new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [new TextRun({ text: "Стр. ", size: 18, color: C.text2 }), new TextRun({ children: [PageNumber.CURRENT], size: 18, color: C.text2 })],
      })] }) },
      children: [
        // --- PROBLEM ---
        h1("Проблема"),
        p("AI-ассистенты (Claude Code, Claude Desktop, Cursor, Windsurf и другие) теряют весь контекст после завершения сессии. Каждый новый разговор начинается с чистого листа."),
        p("Это означает, что:"),
        li("Решения, найденные в прошлых сессиях, приходится искать заново"),
        li("Пароли, IP-адреса, конфигурации серверов нужно каждый раз вводить вручную"),
        li("Контекст проекта (архитектура, договорённости, история багов) теряется"),
        li("Нет преемственности между сессиями \u2014 нельзя продолжить с того места, где остановился"),

        // --- SOLUTION ---
        h1("Решение"),
        p("memory-compiler \u2014 это персональный сервер базы знаний, который работает по протоколу MCP (Model Context Protocol). Он подключается к любому AI-ассистенту и даёт ему долговременную память."),
        quote("AI-ассистент ищет похожие кейсы перед началом задачи и автоматически записывает новые решения после \u2014 без участия пользователя."),
        p("Данные хранятся в виде markdown-статей, организованных по проектам. Каждая статья версионируется через Git. Доступ \u2014 через MCP-протокол, HTTP API или встроенный веб-интерфейс."),

        // --- HOW IT WORKS ---
        h1("Как это работает"),
        h2("Рабочий цикл"),
        tbl(
          ["Этап", "Что происходит"],
          [
            ["Начало задачи", "Ассистент автоматически ищет в базе похожие кейсы и загружает контекст предыдущей сессии"],
            ["В процессе работы", "Ассистент задаёт вопросы к базе знаний и получает ответы с цитатами из конкретных статей"],
            ["Задача решена", "Решение автоматически сохраняется: создаётся новая статья или дополняется существующая"],
            ["Конец сессии", "Контекст сохраняется (что сделано, какие решения приняты, что осталось) для следующей сессии"],
          ],
          [2000, 7360]
        ),

        h2("Гибридный поиск"),
        p("Система использует два поисковых движка одновременно:"),
        li("BM25F \u2014 полнотекстовый поиск с весами: название статьи важнее тегов, теги важнее тела"),
        li("Semantic search \u2014 поиск по смыслу через нейросетевые embeddings, понимает русский и английский"),
        p("Результаты объединяются в единый рейтинг. Дополнительно применяется temporal decay: статьи, к которым обращались недавно, получают бонус."),

        h2("Умное сохранение"),
        p("При сохранении новой записи автоматически выполняется цепочка из 8 шагов:"),
        ni("Запись в дневной лог (аудит-трейл)"),
        ni("Автоматическое добавление тегов по содержанию (14 правил)"),
        ni("Поиск существующей статьи по смыслу \u2014 если найдена похожая, контент мержится"),
        ni("Создание новой статьи или дополнение существующей"),
        ni("Проверка на противоречия с другими статьями (IP-адреса, версии, URL)"),
        ni("Добавление перекрёстных ссылок в связанные статьи"),
        ni("Обновление ленты активного контекста проекта"),
        ni("Автоматический Git-коммит"),

        // --- FEATURES ---
        new PageBreak(),
        h1("Возможности"),

        h2("16 инструментов для AI-ассистента"),
        p("Инструменты разделены на 4 группы:"),
        tbl(
          ["Группа", "Инструменты", "Назначение"],
          [
            ["Поиск", "search, ask, search_by_tag, read_article, get_context, get_summary", "Найти информацию, задать вопрос, получить контекст"],
            ["Запись", "save_lesson, edit_article, delete_article", "Создать, обновить или удалить статью"],
            ["Сессии", "save_session, load_session, get_active_context", "Передача контекста между сессиями"],
            ["Обслуживание", "compile, lint, reindex, article_history", "Компиляция логов, проверка здоровья, история"],
          ],
          [1600, 3200, 4560]
        ),

        h2("Веб-интерфейс"),
        p("Встроенный мобильный веб-интерфейс с тёмной и светлой темой. Доступен по адресу сервера."),
        tbl(
          ["Вкладка", "Функционал"],
          [
            ["Поиск", "Полнотекстовый + семантический поиск, фильтр по проекту, кликабельные теги, просмотр статей с markdown-рендерингом"],
            ["Добавить", "Форма ручного создания записи: тема, проект, теги, содержание"],
            ["Граф знаний", "Интерактивная визуализация связей между статьями. Цвет \u2014 по проекту, размер \u2014 по частоте обращений. Клик по узлу открывает статью"],
            ["Компиляция", "Превью и запуск компиляции дневных логов в структурированные статьи"],
            ["Аналитика", "Топ статей по обращениям, неиспользуемые статьи, общая статистика базы"],
          ],
          [2000, 7360]
        ),

        h2("REST API"),
        p("14 HTTP-endpoints для интеграции с любыми внешними системами: поиск, сохранение, экспорт проекта в JSON, удаление, просмотр тегов, граф знаний, аналитика, управление компиляцией."),

        h2("Автоматизация"),
        li("Автокомпиляция \u2014 ежедневно в 02:00 дневные логи компилируются в структурированные статьи"),
        li("Git-версионирование \u2014 каждое изменение коммитится автоматически, полная история"),
        li("Кэширование embeddings \u2014 быстрый старт за ~45 секунд"),
        li("Автотегирование \u2014 14 правил автоматически добавляют теги по содержанию"),
        li("Уведомления об устаревших статьях \u2014 при загрузке сессии"),

        // --- USE CASES ---
        new PageBreak(),
        h1("Кому полезно"),

        h2("IT-аутсорсинг и сервисные компании"),
        li("Накопление базы решений по клиентским задачам"),
        li("Хранение конфигураций серверов, доступов, IP-адресов"),
        li("Передача контекста между инженерами через сессии"),
        li("Автоматическое обнаружение противоречий (сменился IP, версия, URL)"),

        h2("Разработчики и DevOps"),
        li("Хранение архитектурных решений, workaround-ов, багфиксов"),
        li("Быстрый поиск по всем проектам одновременно"),
        li("Git-история каждой статьи \u2014 кто, когда и что менял"),
        li("Интеграция с Claude Code, Cursor, любым MCP-клиентом"),

        h2("Команды с множеством проектов"),
        li("Организация знаний по проектам: 1С, инфраструктура, конкретные продукты"),
        li("Сводка по проекту за 200 токенов \u2014 быстрый онбординг"),
        li("Граф знаний \u2014 визуальная карта связей между статьями и проектами"),
        li("Экспорт проекта в JSON для бэкапа или передачи"),

        // --- TECH ---
        h1("Технические характеристики"),
        tbl(
          ["Параметр", "Значение"],
          [
            ["Язык сервера", "Python 3.12"],
            ["Фреймворк", "Starlette + Uvicorn (ASGI)"],
            ["MCP-транспорт", "SSE (Server-Sent Events)"],
            ["Полнотекстовый поиск", "Whoosh 2.7 (BM25F)"],
            ["Семантический поиск", "sentence-transformers (paraphrase-multilingual-MiniLM-L12-v2)"],
            ["Хранилище", "Markdown-файлы + Git"],
            ["Контейнеризация", "Docker (Synology NAS, любой Linux-хост)"],
            ["Размер кода", "2235 строк (один файл server.py)"],
            ["Зависимости", "6 Python-пакетов"],
            ["Время старта", "~45 секунд (загрузка модели + индексация)"],
            ["Веб-интерфейс", "Встроенный, мобильный, тёмная/светлая тема"],
            ["Языки поиска", "Русский, английский (любой на основе латиницы/кириллицы)"],
          ],
          [3500, 5860]
        ),

        // --- DEPLOYMENT ---
        h1("Развёртывание"),
        p("Приложение запускается как Docker-контейнер. Данные хранятся в примонтированной директории (Docker volume). Для подключения достаточно указать URL SSE-endpoint в конфигурации MCP-клиента."),
        p("Поддерживаемые платформы:"),
        li("Synology NAS (DS220+, DS920+ и другие с поддержкой Docker)"),
        li("Любой Linux-сервер с Docker"),
        li("Windows / macOS через Docker Desktop"),
        p("Подключение к AI-ассистенту \u2014 одна строка в конфигурации:"),
        new Paragraph({
          spacing: { before: 80, after: 120 },
          shading: { fill: C.bg, type: ShadingType.CLEAR },
          border: { left: { style: BorderStyle.SINGLE, size: 6, color: C.accent, space: 8 } },
          indent: { left: 200 },
          children: [new TextRun({ text: '{"type": "sse", "url": "http://<host>:8765/sse"}', size: 20, font: "Consolas", color: C.accentDark })]
        }),

        // --- HISTORY ---
        h1("История развития"),
        tbl(
          ["Версия", "Дата", "Строк", "Что добавлено"],
          [
            ["v1", "12.04.2026", "~400", "Базовый MCP: сохранение, поиск, контекст"],
            ["v2", "12.04.2026", "~800", "Компиляция в wiki, lint, BM25F, семантика, веб-интерфейс"],
            ["v3", "13.04.2026", "1834", "Сессии, temporal decay, Q&A, граф знаний, аналитика, противоречия"],
            ["v4", "13.04.2026", "2235", "CRUD, теги, история, автотеги, тема, экспорт, markdown"],
          ],
          [1000, 1600, 1000, 5760]
        ),
      ]
    }
  ]
});

Packer.toBuffer(doc).then(buf => {
  fs.writeFileSync("C:\\Users\\areli\\SynologyDrive\\DEV\\memory-compiler\\docs\\memory-compiler-overview.docx", buf);
  console.log("OK: memory-compiler-overview.docx");
});
