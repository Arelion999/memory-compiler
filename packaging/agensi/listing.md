# Memory Compiler — Persistent Project Memory

**Price:** Free

**Tagline:** Persistent project memory through your own Memory Compiler MCP server.

**Description:** Keep useful project context available across assistant sessions with Memory Compiler, an open-source, self-hosted knowledge base. This skill guides an AI agent to select a project, retrieve relevant notes, and record useful findings through Memory Compiler MCP tools. Search and write only when relevant; it does not promise perfect recall or replace checking current evidence.

**Prerequisites:** A separately installed and configured Memory Compiler server, connected to the AI host as an MCP server; the host must support importing and using skills. Setup instructions are in the [English README](https://github.com/Arelion999/memory-compiler/blob/master/README.md). The server is licensed under MIT; see the [source repository](https://github.com/Arelion999/memory-compiler).

**Limitations:** Agensi delivers this skill through its connector; it is a companion skill, not the server. It does not install or host the server, configure MCP connections, add host hooks, or automatically collect data. Memory availability depends on the user's server, connection, and host. The user controls the endpoint where memory is stored.

**Suggested category:** Agents & Orchestration

**Suggested tags:** memory, MCP, knowledge base, self-hosted, project context

**Use cases:**
- Resume substantive project work with relevant prior context.
- Reuse recorded bug fixes and project decisions.
- Retain verified outcomes for future sessions.

**Example prompts:**
- “Look up our prior decisions about the deployment before changing it.”
- “Start this task with the project notes, then record the verified outcome when finished.”
- “Find the article about this error in my Memory Compiler.”

**Illustrative example:** After a completed task, an agent might say: “I added the verified migration result to the `billing-api` project notes.” This is an example only; a write is reported as saved only after the connected server confirms it.
