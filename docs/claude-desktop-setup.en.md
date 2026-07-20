# Setting up memory-compiler in Claude Code Desktop

**English** · [Русский](claude-desktop-setup.md)

Integration via MCP plus the memory-autopilot skill. Written for Windows (paths of the form `C:\Users\<user>`), but works on macOS/Linux too (substitute the paths).

## Contents

1. [Starting the server](#1-starting-the-server)
2. [Connecting MCP](#2-connecting-mcp)
3. [Installing the memory-autopilot skill](#3-installing-the-memory-autopilot-skill)
4. [Global CLAUDE.md](#4-global-claudemd)
5. [Hooks (a safety net)](#5-hooks-a-safety-net)
6. [Verifying it works](#6-verifying-it-works)
7. [Configuring project dependencies](#7-configuring-project-dependencies)
8. [Claude Desktop limitations](#8-claude-desktop-limitations)

---

## 1. Starting the server

**Locally (Docker):**
```bash
git clone https://github.com/Arelion999/memory-compiler.git
cd memory-compiler
cp .env.example .env
# Fill in MC_API_KEY and MC_ENCRYPT_KEY in .env
docker-compose up -d
```

**On a NAS (Synology):**
- Mount the repo in SynologyDrive
- Create a `.env` with `MC_API_KEY` and `MC_ENCRYPT_KEY` (optionally `GIT_REPOS_PATH`, `OBSIDIAN_VAULT_PATH`)
- Start it through Container Manager or over SSH: `docker-compose up -d`

**Check:**
```bash
curl http://localhost:8765/api/health
# {"status": "ok", ...}
```

---

## 2. Connecting MCP

Add the server to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "memory-compiler": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://<host>:8765/sse?key=<MC_API_KEY>",
        "--allow-http",
        "--transport",
        "sse-only"
      ]
    }
  }
}
```

**Important:**
- URL-encode special characters in the key (`$` → `%24`)
- `--transport sse-only` is mandatory (without it the fallback causes timeouts)
- `--allow-http` is only needed for an unencrypted connection (local network)

---

## 3. Installing the memory-autopilot skill

The skill automates the entire memory workflow: it looks up context, determines the project, picks the tool and saves the result. The user never thinks about memory — the skill handles everything.

**Installation:**
```bash
# From the memory-compiler repo
mkdir -p ~/.claude/skills/memory-autopilot
cp skills/memory-autopilot/SKILL.md ~/.claude/skills/memory-autopilot/SKILL.md
```

**What the skill does:**
- Triggers automatically on tasks, facts, questions and errors
- Determines the project from context (mapping table inside)
- Picks the right tool from a decision tree (save_lesson / save_decision / save_runbook / save_from_template / save_secret)
- Calls start_task at the beginning and finish_task at the end
- Looks up credentials and context in the base without asking the user

⚠️ **SKILL.md is currently Russian-only.** The skill still works with an English-speaking user — the model reads the instructions and answers in the user's language — but its internal wording, its examples and the trigger phrases it lists are Russian.

**Configuring projects:** edit the "Определение проекта" (project routing) table in SKILL.md to match your own projects.

---

## 4. Global CLAUDE.md

Path: `~/.claude/CLAUDE.md`

A minimal template (the skill carries most of the load):

```markdown
# Global rules

## Memory (memory-compiler MCP)

Memory management is automated through the `memory-autopilot` skill.
The skill calls start_task and finish_task itself, and picks the tool and the project.

**FORBIDDEN:** using mcp__memory (create_entities, add_observations, etc.) — that is the legacy system.
For ALL memory operations use ONLY memory-compiler tools (mcp__memory-compiler__*).
```

---

## 5. Hooks (a safety net)

The memory-autopilot skill replaces most hooks. Keep only two:

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "echo '{\"hookSpecificOutput\":{\"hookEventName\":\"Stop\",\"decision\":\"block\",\"reason\":\"STOP. Was a non-trivial task solved? If YES and you did NOT call memory-compiler:finish_task — CALL IT NOW.\"}}'",
        "timeout": 5
      }]
    }],
    "PostCompact": [{
      "hooks": [{
        "type": "command",
        "command": "echo '{\"hookSpecificOutput\":{\"hookEventName\":\"PostCompact\",\"additionalContext\":\"CONTEXT WAS COMPACTED. If there were non-trivial decisions that are not recorded — call memory-compiler:finish_task now.\"}}'",
        "timeout": 5
      }]
    }]
  }
}
```

- **Stop** — a safety net: blocks the end of a session if finish_task was never called
- **PostCompact** — a reminder when the context is compacted

---

## 6. Verifying it works

After installing, **restart** Claude Code Desktop.

1. **The skill is loaded:**
   `memory-autopilot` should appear in the skill list.

2. **MCP is reachable:**
   Say "check that the knowledge base is available" — the skill should call `list_projects`.

3. **A trial cycle:**
   - State a fact: "server X is at site Y" — the skill should store it via `save_lesson`
   - Give it a task: "set up nginx for a new site" — the skill should call `start_task`, pull in context, and finish with `finish_task`

---

## 7. Configuring project dependencies

`start_task` automatically pulls context from dependent projects:

```python
set_project_deps(project="client-a", depends_on=["work", "infra"])
set_project_deps(project="myapp", depends_on=["infra", "work"])
```

---

## 8. Claude Desktop limitations

| Feature | Status | Comment |
|---------|--------|---------|
| MCP tools (all 46) | ✅ | Fully supported |
| memory-autopilot skill | ✅ | Auto-triggered by its description |
| Stop/PostCompact hooks | ✅ | Safety net |
| Auto search_error on a traceback | ✅ | Through the skill (Phase 0) |
| Auto git_capture after a commit | ❌ | No FileChanged event in Desktop |

---

## Troubleshooting

**The skill does not trigger?**
- Make sure `~/.claude/skills/memory-autopilot/SKILL.md` exists
- Restart Desktop
- Check that the skill is in the list (its description starts with "Use ALWAYS")

**An MCP tool is unavailable?**
- Check the server: `curl http://<host>:8765/api/health`
- Check `MC_API_KEY` in the URL (URL-encode special characters)
- Desktop logs: `%APPDATA%\Claude\logs\`

**The Stop hook is blocking?**
- Claude has to call finish_task to unblock
- If it gets stuck, comment out the Stop hook temporarily
