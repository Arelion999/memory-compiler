# memory-compiler security

<!-- Ссылка АБСОЛЮТНАЯ намеренно, не «забыли сократить». GitHub рендерит этот файл
     по двум адресам: /blob/master/docs/security.md (база — docs/) и /security/policy
     (база — КОРЕНЬ репо). Относительная `security.ru.md` во втором случае разрешается
     в /blob/master/security.ru.md и даёт 404. Одной относительной ссылки, работающей
     в обеих, не существует. Стережёт tests/test_docs_i18n.py::ABSOLUTE_SWITCHER. -->
**English** · [Русский](https://github.com/Arelion999/memory-compiler/blob/master/docs/security.ru.md)

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private channel:
the repository's **Security** tab → **Report a vulnerability**.

The thread is visible only to you and the maintainer; once fixed,
an advisory is published from it.

## Architecture

```
┌──────────────────┐     ┌──────────────────┐     ┌───────────────┐
│  Claude Code /   │     │    Phone / PC    │     │    Docker     │
│  Desktop (MCP)   │     │    (Web UI)      │     │  healthcheck  │
└────────┬─────────┘     └────────┬─────────┘     └──────┬────────┘
         │                        │                      │
     X-Api-Key            cookie mc_token             no key
         │                        │                      │
         ▼                        ▼                      ▼
┌────────────────────────────────────────────────────────────┐
│                    AuthMiddleware (ASGI)                   │
│                                                            │
│  /api/health          → allow through (public)             │
│  /login               → allow through (login page)         │
│  /.well-known/*       → 404 (OAuth discovery)              │
│  /mcp                 → Bearer or X-Api-Key header         │
│  /sse (legacy)        → Bearer or ?key= in the URL         │
│  /messages/*          → session_id from the /sse stream    │
│  everything else      → Bearer or cookie                   │
│                                                            │
│  No key → 401 (API) or redirect to /login (browser)        │
└────────────────────────────────────────────────────────────┘
```

## Layer 1: Authorisation

A single key, supplied through the `MC_API_KEY` environment variable.

| Client | How it passes the key |
|--------|-----------------------|
| Claude Code / Claude Desktop (MCP) | `X-Api-Key` header (or `Authorization: Bearer`) to `/mcp`, set once in the config |
| Browser (PC/phone) | Login page → `mc_token` cookie for 30 days |
| REST API (curl) | `Authorization: Bearer xxx` (`?key=` is not accepted for REST) |
| Docker healthcheck | No key — `/api/health` is public |

If `MC_API_KEY` is unset, the server runs without authorisation (backwards compatibility).

### Configuring Claude Desktop

```json
{
  "mcpServers": {
    "memory-compiler": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "http://<NAS_IP>:8765/mcp",
        "--allow-http",
        "--header", "X-Api-Key:<your-key>",
        "--transport", "http-only"
      ]
    }
  }
}
```

The header value must not contain a space: on Windows, Desktop launches `mcp-remote` through `cmd.exe`, and a space splits the command. That is why the key goes in `X-Api-Key` rather than `Authorization: Bearer <key>`.

Claude Code connects directly, without `mcp-remote`: `"type": "http"`, `"url": "http://<NAS_IP>:8765/mcp/"`, `"headers": {"X-Api-Key": "<your-key>"}`.

### Configuring Docker

Create a `.env` next to `docker-compose.yml`:

```env
MC_API_KEY=your-secret-key
MC_ENCRYPT_KEY=your-encryption-key
```

`docker-compose.yml` picks up `.env` automatically.

### Web UI (phone/PC)

1. Open `http://<NAS_IP>:8765`
2. A key entry form appears
3. Enter `MC_API_KEY`
4. The cookie is stored for 30 days — no need to enter it again

## Layer 2: Secret encryption

Configured through `MC_ENCRYPT_KEY`. Used to encrypt sensitive articles (passwords, keys, credentials).

### How it works

```
save_secret("Server password", "root:P@ss123", project="infra")
                    │
                    ▼
        PBKDF2 (100000 iterations) → Fernet (AES-256)
                    │
                    ▼
        File on disk:   ENC:gAAAAABn...  (unreadable)
        Search index:   title + tags (no content)
                    │
                    ▼
read_article() → decryption → "root:P@ss123"
search()       → "[зашифровано — используй read_article для просмотра]"
```

The placeholder above is emitted verbatim by the server and is currently Russian-only
("encrypted — use read_article to view"); it is a runtime string, not documentation.

### What gets encrypted

- Only articles created through `save_secret`
- Ordinary articles (`save_lesson`) are stored in plain text
- The search index holds only the title and tags of secret articles; the content is not indexed
- Secrets are encrypted in the git history

### Findability of secrets (v1.7.29)

Since a secret's body is not indexed, a secret can only be found by its title and tags.
To make it findable by entity name (login, host, IP), `save_secret` uses
`extract_secret_identifiers` to auto-add **non-secret identifiers** to the tags:
logins (only those following a login keyword: `логин/login/user/пользователь/…`)
and IP addresses. **Password, token and key values never reach the tags:** capture happens
only after login keywords (never after `пароль/password/token/ключ/key/secret`),
a strict identifier pattern rejects password-like strings, and a stop-list removes
generic tokens (`root/admin/ssh`). So `search("<login>")` finds the secret by its
login while the password stays out of the index.

### Without MC_ENCRYPT_KEY

- `save_secret` returns an error
- Ordinary `save_lesson` calls work as before

## Layer 3: Audit

An automatic log of every call to the MCP tools.

### Record format

```json
{"ts": "2026-04-13 22:31:15", "tool": "search", "args": {"query": "docker", "project": "all"}, "size": 1500}
{"ts": "2026-04-13 22:31:20", "tool": "save_lesson", "args": {"topic": "Nginx", "content": "[850 chars]"}, "size": 200}
```

### Masking

Sensitive fields are masked in the log automatically:
- `content` → `[N chars]`
- `error_text` → `[N chars]`
- `steps` → `[N chars]`
- `password`, `key` → `***`

### Accessing the audit log

- Web UI → "Audit" tab (last 100 records)
- REST API → `GET /api/audit`
- File → `knowledge/_audit.log` (JSON lines)

## Protection matrix

| What | Protection | Note |
|------|------------|------|
| Web UI | Login + 30-day cookie | Redirects to /login without a cookie |
| REST API | Bearer / cookie | 401 without a key |
| MCP (`/mcp`) | Bearer / `X-Api-Key` header | Configured once in the config file |
| MCP legacy (`/sse`) | Bearer / `?key=` in the URL | Kept for old configs |
| /api/health | Public | For the Docker healthcheck |
| Secret articles | AES-256 on disk | Only via save_secret |
| Ordinary articles | Plain text | Not encrypted |
| Git history | Secrets encrypted | Ordinary articles in plain text |
| Audit | Automatic | `content` is masked |
| HTTP traffic | **Not encrypted** | Local network only |

## Technical details

### Why pure ASGI middleware

Starlette's `BaseHTTPMiddleware` is incompatible with SSE — it raises `TypeError: 'NoneType' object is not callable` on disconnect. AuthMiddleware is implemented as pure ASGI middleware instead.

### Why the key goes in the X-Api-Key header

`/mcp` takes the key from `Authorization: Bearer` or from `X-Api-Key`, and `mcp-remote` passes either with `--header`. On Windows, Claude Desktop launches `mcp-remote` through `cmd.exe`, and the space in `Bearer <key>` splits the command: the process dies right after start. `X-Api-Key` has no space in its value and gets through intact.

`/mcp` does not accept `?key=`: a key in the URL leaks into access logs, the `Referer` header and browser history. The legacy `/sse` still takes it so that old configs keep working.

### Why --transport http-only

By default (`http-first`) `mcp-remote` falls back to the legacy SSE transport on a 404 or 405. With `http-only` it connects straight to Streamable HTTP, and a failure shows up as a failure instead of being masked by the fallback.

Legacy SSE has a flaw of its own. A client that loses its stream reconnects by itself, gets a new session and does not repeat `initialize`. Before v1.90.1 the server answered every call on such a session with `-32602 Invalid request parameters`, while the client kept showing the server as connected. Since v1.90.1 an SSE session counts as initialised from the start (the SDK's `stateless` flag), and these calls are served.

### Why /.well-known/ returns 404

On start-up `mcp-remote` attempts OAuth discovery against `/.well-known/oauth-authorization-server`. If the middleware returns 401, `mcp-remote` treats it as an authorisation failure. A 404 is the correct "OAuth is not supported" answer.
