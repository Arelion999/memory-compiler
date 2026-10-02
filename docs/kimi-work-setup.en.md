# Setting up memory-compiler in Kimi Work

**English** · [Русский](kimi-work-setup.md)

Kimi Work (desktop, the daimon engine) connects as a plugin: the MCP server,
the memory-autopilot skill and the hooks live in a single package, and the
runtime executes the hooks itself. Instructions for Windows/macOS/Linux.

## Contents

1. [Starting the server](#1-starting-the-server)
2. [Creating the plugin and connecting MCP](#2-creating-the-plugin-and-connecting-mcp)
3. [Installing the memory-autopilot skill](#3-installing-the-memory-autopilot-skill)
4. [AGENTS.md of the working directories](#4-agentsmd-of-the-working-directories)
5. [Hooks: the mc_guard memory gate](#5-hooks-the-mc_guard-memory-gate)
6. [Verifying the setup](#6-verifying-the-setup)
7. [Project dependencies](#7-project-dependencies)
8. [What Kimi Work supports](#8-what-kimi-work-supports)

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

**Check:**
```bash
curl http://<host>:8765/api/health
# {"status": "ok", ...}
```

The server can live on another machine (a NAS, a host on the LAN) — the
plugin only needs the URL and the key.

---

## 2. Creating the plugin and connecting MCP

Kimi Work connects MCP servers through personal-market plugins. Create a
plugin directory (outside the memory-compiler repository, e.g. in your own
tools directory):

```json
// <plugin-dir>/kimi.plugin.json
{
  "name": "memory-compiler",
  "version": "0.1.0",
  "description": "Personal knowledge base memory-compiler",
  "skills": "./skills/",
  "mcpServers": {
    "memory-compiler": {
      "url": "http://<host>:8765/mcp/",
      "headers": { "X-Api-Key": "<MC_API_KEY>" }
    }
  },
  "sessionStart": { "skill": "memory-autopilot" }
}
```

Copy the skill into the plugin (it will be needed later too):

```bash
mkdir -p <plugin-dir>/skills/memory-autopilot
cp skills/memory-autopilot/SKILL.md <plugin-dir>/skills/memory-autopilot/SKILL.md
```

Register the plugin in the personal market and install it:

```bash
# Registration (the path to kimi-daimon depends on the client installation)
kimi-daimon kimi-plugin register-personal <plugin-dir> --share-dir <daimon-share>
```

Then: **Settings → Plugins → the Personal tab → install**. The daemon
applies the installation to active sessions — no client restart needed.

The `sessionStart` field is a native replacement for a SessionStart hook:
the runtime surfaces the memory-autopilot skill at the start of every
session on its own.

---

## 3. Installing the memory-autopilot skill

The skill automates the whole memory cycle: it looks up context, determines
the project, picks the tool and saves the result.

Kimi Work reads user skills from `~/.agents/skills` — put a copy there:

```bash
mkdir -p ~/.agents/skills/memory-autopilot
cp skills/memory-autopilot/SKILL.md ~/.agents/skills/memory-autopilot/SKILL.md
```

The copy inside the plugin (step 2) handles auto-loading at session start;
the copy in `~/.agents/skills` handles triggering by context in any working
directory. The two are identical — keep them in sync when updating.

**Project setup:** edit the "Project identification" table in SKILL.md for
your own projects.

---

## 4. AGENTS.md of the working directories

Kimi Work has no global AGENTS.md — the client reads the AGENTS.md of the
working directory. Add the rule to the top of the AGENTS.md of every
project the assistant works in:

```markdown
# <project>

🛑 FIRST ACTION OF ANY TASK — memory: the memory-autopilot skill (or
start_task/search from the memory-compiler plugin tools directly). Kimi Work
has no reminding hooks until mc_guard is installed (step 5) — nobody will
remind the model; the duty is entirely on it.
```

After installing mc_guard (step 5) this item remains useful as the first
line of defence, while the gate becomes the second.

---

## 5. Hooks: the mc_guard memory gate

The skill is a soft channel: it fires when the model invokes it. The hard
channel is hooks: Kimi Work executes `hooks` declared in the plugin manifest
itself (SessionStart, UserPromptSubmit, PreToolUse, Stop, PostToolUse and
others; PreToolUse can block a call with a reason).

The `hooks/mc_guard.py` guard from the memory-compiler repository supports
Kimi Work. One command from the repository directory:

```bash
python hooks/install.py --client=kimiwork
```

The installer copies `mc_guard.py` and `mc_guard.env` (server address and
key — edit it after installation) into `<plugin-dir>/hooks`, writes 7 hooks
into `kimi.plugin.json` and bumps the plugin version. After that:

1. re-register the plugin (`kimi-plugin register-personal`, as in step 2);
2. in the client, press **update** on the plugin in the Personal tab.

What the plugin hooks include:

| Event | Subcommand | What it does |
|---|---|---|
| `SessionStart` | `session_start` | starting context from the base |
| `UserPromptSubmit` | `freshness` | other sessions' writes + the memory reminder |
| `PreToolUse` | `nul_guard` | blocks redirects into `nul`/`con` (Windows) |
| `PreToolUse` | `gate` | **first call of ANY tool is blocked until the session has read the base within 15 min** |
| `PreToolUse` | `session_arg` | passes the chat id to the server (context freshness) |
| `Stop` | `stop` | backstop: don't let the session close without finish_task |
| `PostToolUse` | `mark` | marks "base read" (clears the gate) |

The gate's wide matcher covers everything except the memory-compiler tools
themselves and the `Skill`/`select_tools` loaders (without them the
memory-compiler tools could not even be loaded). Any other first call —
`Bash`, `Read`, `TodoList`, an infra MCP tool — is denied with an
instruction to run `start_task` (or `search` for a trivial question); the
repeat then goes through. Infrastructure targets keep the card shortcut:
if the base already has a knowledge card for the target, the call passes
with the card instead of a block.

Tool names in Kimi Work look like `mcp__plugin-<plugin>_<server>__<tool>` —
mc_guard normalises them back to the usual `mcp__<server>__<tool>` form by
itself; no matchers to maintain. Tests: `MC_GUARD_CLIENT=kimi python hooks/test_mc_guard.py`.

**Managed copy.** The daimon runtime reads the manifest and the script not
from `plugin-sources` but from the loaded copy at
`daimon/runtime/kimi-code/home/plugins/managed/<plugin>/`, and it
re-reads the manifest only at app start. `install.py --client=kimiwork`
syncs that copy itself (with `.bak-<timestamp>` backups), but **after
installing hook changes a Kimi Work restart is required** — new chats in a
running client still use the hooks loaded at startup. Symptom of a stale
copy: the gate stays silent in a fresh chat and no entries appear in
`mc_hooks.log` after the install.

---

## 6. Verifying the setup

In a **fresh** Kimi Work chat:

1. **Skill in the index:** the session's skill list should contain
   `memory-autopilot`.
2. **MCP available:** "check the knowledge base availability" → a
   `list_projects` call.
3. **Gate (after step 5):** in a fresh chat ask for something trivial like
   `git status` — the FIRST call of any tool is blocked with an instruction
   to run `start_task` first; after `search`/`start_task` the call goes
   through. Visible in the `~/.kimi-code/hooks/mc_hooks.log` journal
   (`gate.block` / `gate.pass` entries). If a fresh chat shows no reaction
   and the journal has no new entries — the manifest was not re-read:
   restart Kimi Work (see "Managed copy" above).
4. **Trial cycle:** state a fact ("server X at site Y") — the skill saves it
   via `save_lesson`; give it a task — the skill calls `start_task` and
   `finish_task` at the end.

---

## 7. Project dependencies

`start_task` automatically pulls context from dependent projects:

```python
set_project_deps(project="client-a", depends_on=["work", "infra"])
set_project_deps(project="myapp", depends_on=["infra", "work"])
```

---

## 8. What Kimi Work supports

| Feature | Status | Comment |
|---------|--------|---------|
| MCP tools (all 50) | ✅ | Via the plugin, Streamable HTTP |
| memory-autopilot skill | ✅ | Auto-trigger + plugin `sessionStart` |
| Gate hooks (gate/stop/freshness) | ✅ | Natively: `hooks` in the plugin manifest |
| Blocking a call with a reason | ✅ | `PreToolUse` + `permissionDecision: deny` |
| Auto-search_error on traceback | ✅ | Through the skill (Phase 0) |
| Argument rewriting (`updatedInput`) | ⚠️ | The protocol supports it; mc_guard does not rely on it — there is a side channel `/api/session_hint` |

---

## Troubleshooting

**Plugin tools don't show up?**
- Is the plugin installed in the Personal tab (not just registered)?
- Server is alive: `curl http://<host>:8765/api/health`
- The key in `headers.X-Api-Key` is correct.

**Hooks don't fire?**
- After `install.py --client=kimiwork`, was the plugin re-registered AND
  updated in the client? Hooks are read from the installed copy, not the
  sources.
- Journal: `tail -f ~/.kimi-code/hooks/mc_hooks.log` — every hook run is
  visible there.
- `mc_guard.env` next to the copied script is filled in (`MC_API_URL`,
  `MC_API_KEY`).

**Gate blocks forever?**
- There is a valve: after two consecutive blocks the call is let through
  (a `gate.valve` entry in the journal). If the base is unavailable — say
  so instead of inventing facts from memory.
