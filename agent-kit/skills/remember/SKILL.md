---
name: remember
description: Save or look up notes in the user's shared memory (Agent Bullpen), which every AI agent and account on every machine shares. Use when the user says /remember, asks you to remember something for later or "for all agents", asks what you (or other agents) remember, or when you learn a durable fact, preference or decision that other agents will need.
---

# Shared memory

The user runs several AI agents (for example Claude Code on one or more
accounts, Gemini CLI and Antigravity) on several machines. They share one memory
on the Agent Bullpen. Notes the user pinned are already in your context, under
"Shared memory (managed by Agent Bullpen)".

## Saving (`/remember …`)

1. Turn the request into one short, self-contained note that still makes sense
   to a different agent on a different machine months from now. Name things
   explicitly ("on build-box", "in the web-app repo"), and include the why when it
   matters. One fact or decision per note; save several notes for several facts.
2. Pick a scope:
   - `global`: about the user, their preferences or their whole setup (default);
   - `project:<name>`: only about one project;
   - `agent:<name>`: only for one agent, e.g. `agent:claude-work`.
3. Call the **`remember`** tool of the **`bullpen`** MCP server (it may be listed as
   `mcp__bullpen__remember` or `mcp_bullpen_remember`) with `text`, `scope` and a few
   lowercase `tags`. Only the user can pin notes (pinned notes are loaded into every
   session everywhere). If the user asks to pin it, pass `pin: true`: that marks the
   note "pin requested" for them to approve on the hub's Memory page.
4. Confirm in one line what you saved and where (scope; pin requested or not).

If the `bullpen` tools aren't available, run this shell command instead:

    bullpen remember --scope global --tags "tag1,tag2" "the note text"

(add `--pin` to request pinning). Never save secrets such as passwords, API keys or tokens:
tell the user that, and suggest where they should keep them instead.

## Looking things up

When the user asks what you remember, or before asking them something they may
already have told another agent, call the **`recall`** tool of the `bullpen` server
with a few keywords (or run `bullpen recall <words>`). Say which notes you used.

<!-- managed-by: bullpen -->
