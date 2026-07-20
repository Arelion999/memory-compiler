# memory-compiler security

**English** · [Русский](security.md)

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private channel:
the repository's **Security** tab → **Report a vulnerability**.

The thread is visible only to you and the maintainer; once fixed,
an advisory is published from it.

## Architecture

```
┌──────────────────┐     ┌──────────────────┐     ┌───────────────┐
│  Claude Desktop  │     │    Phone / PC    │     │    Docker     │
│  (MCP tools)     │     │    (Web UI)      │     │  healthcheck  │
└────────┬─────────┘     └────────┬─────────┘     └──────┬────────┘
         │                        │                      │
     ?key=xxx             cookie mc_token             no key
         │                        │                      │
         ▼                        ▼                      ▼
┌────────────────────────────────────────────────────────────┐
│                    AuthMiddleware (ASGI)                   │
│                                                            │
│  /api/health          → allow through (public)             │
│  /login               → allow through (login page)         │
│  /.well-known/*       → 404 (OAuth discovery)              │
│  /sse, /messages/*    → check ?key= in the URL             │
│  everything else      → check Bearer / cookie / ?key=      │
│                                                            │
│  No key → 401 (API) or redirect to /login (browser)        │
└────────────────────────────────────────────────────────────┘
```

## Layer 1: Authorisation

A single key, supplied through the `MC_API_KEY` environment variable.

| Client | How it passes the key |
|--------|-----------------------|
| Claude Desktop (MCP) | `?key=` in the SSE connection URL (set once in the config) |
| Browser (PC/phone) | Login page → `mc_token` cookie for 30 days |
| REST API (curl) | `Authorization: Bearer xxx` or `?key=xxx` |
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
        "http://<NAS_IP>:8765/sse?key=<your-key>",
        "--allow-http",
        "--transport", "sse-only"
      ]
    }
  }
}
```

If the key contains special characters (`$`, `&`, `#`), URL-encode them (`$` → `%24`).

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
| REST API | Bearer / cookie / ?key= | 401 without a key |
| MCP SSE | ?key= in the URL | Configured once in the config file |
| /api/health | Public | For the Docker healthcheck |
| Secret articles | AES-256 on disk | Only via save_secret |
| Ordinary articles | Plain text | Not encrypted |
| Git history | Secrets encrypted | Ordinary articles in plain text |
| Audit | Automatic | `content` is masked |
| HTTP traffic | **Not encrypted** | Local network only |

## Technical details

### Why pure ASGI middleware

Starlette's `BaseHTTPMiddleware` is incompatible with SSE — it raises `TypeError: 'NoneType' object is not callable` on disconnect. AuthMiddleware is implemented as pure ASGI middleware instead.

### Why ?key= in the URL rather than a header

`mcp-remote` (the proxy for MCP over SSE) does not support custom headers (there is no `--header` flag). The only way to pass the key is a URL query parameter.

### Why --transport sse-only

Without this flag `mcp-remote` first tries an HTTP POST to `/sse` → gets a 405 → falls back to SSE. With `--transport sse-only` it connects directly.

### Why /.well-known/ returns 404

On start-up `mcp-remote` attempts OAuth discovery against `/.well-known/oauth-authorization-server`. If the middleware returns 401, `mcp-remote` treats it as an authorisation failure. A 404 is the correct "OAuth is not supported" answer.
