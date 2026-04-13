# Безопасность memory-compiler

## Архитектура

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────┐
│  Claude Desktop  │     │   Телефон/ПК     │     │  Docker      │
│  (MCP tools)     │     │   (Web UI)       │     │  healthcheck │
└────────┬────────┘     └────────┬─────────┘     └──────┬──────┘
         │                       │                       │
    ?key=xxx              cookie mc_token          без ключа
         │                       │                       │
         ▼                       ▼                       ▼
┌────────────────────────────────────────────────────────────┐
│                    AuthMiddleware (ASGI)                    │
│                                                            │
│  /api/health          → пропустить (публичный)             │
│  /login               → пропустить (логин-страница)        │
│  /.well-known/*       → 404 (OAuth discovery)              │
│  /sse, /messages/*    → проверить ?key= в URL              │
│  всё остальное        → проверить Bearer / cookie / ?key=  │
│                                                            │
│  Нет ключа → 401 (API) или redirect /login (браузер)       │
└────────────────────────────────────────────────────────────┘
```

## Уровень 1: Авторизация

Единый ключ задаётся через переменную окружения `MC_API_KEY`.

| Клиент | Как передаёт ключ |
|--------|-------------------|
| Claude Desktop (MCP) | `?key=` в URL SSE подключения (один раз в конфиге) |
| Браузер (ПК/телефон) | Логин-страница → cookie `mc_token` на 30 дней |
| REST API (curl) | `Authorization: Bearer xxx` или `?key=xxx` |
| Docker healthcheck | Без ключа — `/api/health` публичный |

Если `MC_API_KEY` не задан — сервер работает без авторизации (обратная совместимость).

### Настройка Claude Desktop

```json
{
  "mcpServers": {
    "memory-compiler": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "http://<NAS_IP>:8765/sse?key=<ваш-ключ>",
        "--allow-http",
        "--transport", "sse-only"
      ]
    }
  }
}
```

Если в ключе есть спецсимволы (`$`, `&`, `#`), используйте URL-encoding (`$` → `%24`).

### Настройка Docker

Создайте `.env` рядом с `docker-compose.yml`:

```env
MC_API_KEY=ваш-секретный-ключ
MC_ENCRYPT_KEY=ваш-ключ-шифрования
```

`docker-compose.yml` автоматически подхватывает `.env`.

### Web UI (телефон/ПК)

1. Откройте `http://<NAS_IP>:8765`
2. Появится форма ввода ключа
3. Введите `MC_API_KEY`
4. Cookie сохранится на 30 дней — повторный ввод не нужен

## Уровень 2: Шифрование секретов

Задаётся через `MC_ENCRYPT_KEY`. Используется для шифрования чувствительных статей (пароли, ключи, credentials).

### Как работает

```
save_secret("Пароль от сервера", "root:P@ss123", project="infra")
                    │
                    ▼
           PBKDF2 (100000 итераций) → Fernet (AES-256)
                    │
                    ▼
        Файл на диске: ENC:gAAAAABn...  (нечитаемый)
        Индекс поиска: заголовок + теги (без контента)
                    │
                    ▼
read_article() → расшифровка → "root:P@ss123"
search()       → "[зашифровано — используй read_article]"
```

### Что шифруется

- Только статьи созданные через `save_secret`
- Обычные статьи (`save_lesson`) хранятся в plain text
- В поисковом индексе секретных статей — только заголовок и теги, контент не индексируется
- В git history секреты зашифрованы

### Без MC_ENCRYPT_KEY

- `save_secret` вернёт ошибку
- Обычные `save_lesson` работают как раньше

## Уровень 3: Аудит

Автоматический лог всех обращений к MCP tools.

### Формат записи

```json
{"ts": "2026-04-13 22:31:15", "tool": "search", "args": {"query": "docker", "project": "all"}, "size": 1500}
{"ts": "2026-04-13 22:31:20", "tool": "save_lesson", "args": {"topic": "Nginx", "content": "[850 chars]"}, "size": 200}
```

### Маскировка

Чувствительные поля автоматически маскируются в логе:
- `content` → `[N chars]`
- `error_text` → `[N chars]`
- `steps` → `[N chars]`
- `password`, `key` → `***`

### Доступ к аудиту

- Web UI → вкладка "Аудит" (последние 100 записей)
- REST API → `GET /api/audit`
- Файл → `knowledge/_audit.log` (JSON lines)

## Матрица защиты

| Что | Защита | Примечание |
|-----|--------|------------|
| Web UI | Логин + cookie 30 дней | Redirect на /login без cookie |
| REST API | Bearer / cookie / ?key= | 401 без ключа |
| MCP SSE | ?key= в URL | Настраивается один раз в конфиге |
| /api/health | Публичный | Для Docker healthcheck |
| Секретные статьи | AES-256 на диске | Только через save_secret |
| Обычные статьи | Plain text | Не шифруются |
| Git history | Секреты зашифрованы | Обычные статьи в plain text |
| Аудит | Автоматический | content маскируется |
| HTTP трафик | **Не шифруется** | Только для локальной сети |

## Технические детали

### Почему pure ASGI middleware

Starlette `BaseHTTPMiddleware` несовместим с SSE — вызывает `TypeError: 'NoneType' object is not callable` при disconnect. AuthMiddleware реализован как чистый ASGI middleware.

### Почему ?key= в URL а не в заголовке

`mcp-remote` (прокси для MCP через SSE) не поддерживает кастомные заголовки (`--header` флаг отсутствует). Единственный способ передать ключ — через URL query parameter.

### Почему --transport sse-only

Без этого флага `mcp-remote` сначала пробует HTTP POST на `/sse` → получает 405 → fallback на SSE. С `--transport sse-only` подключается сразу.

### Почему /.well-known/ возвращает 404

`mcp-remote` при старте пробует OAuth discovery на `/.well-known/oauth-authorization-server`. Если middleware возвращает 401, `mcp-remote` считает это ошибкой авторизации. 404 — корректный ответ "OAuth не поддерживается".
