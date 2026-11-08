# 11 · Adding other agent CLIs

Agent Bullpen fully supports Claude Code and Gemini CLI: a live log of what the agent does, follow-ups
in the same conversation, a check that the agent is installed and signed in, and the shared notes and MCP
servers in its own settings. Antigravity gets the shared notes and MCP servers, but the hub doesn't run it
([guide 1](01-accounts-and-rules.md#google-why-a-google-ai-plan-cant-drive-the-agents)).

Today you can add any other coding agent that can run without a person at the keyboard, without changing
any code, as a text agent: a few lines in the machine's `runner.json`. This guide shows how, and what full
support adds. The [roadmap](../ROADMAP.md) lists the agents planned next.

## What a text agent can and can't do

| | Text agent (works today) | Full support |
|---|---|---|
| Tasks and council seats | yes | yes |
| Model box and the three autonomy levels | yes, with the flags you give it | yes |
| Live log | the program's output, line by line | readable steps ("Read src/app.py", "Run npm test") |
| Final answer | everything the program printed | just the answer |
| Follow-ups | only if the program accepts a session id you choose (see `new_session` below), or with a matching parser | yes |
| "Is it ready?" on the dashboard | only "the program was found" | also "it's signed in" |
| Shared notes, `/remember`, the MCP list | no: set those up in the program yourself | written into its settings by the runner |

## Before you start

1. **Install the program on the machine and sign it in**, the way its own docs say. Use your own account, and
   check the vendor's rules for automated use ([guide 1](01-accounts-and-rules.md#other-agent-programs)).
2. **Find out how it runs headless.** You need four answers from its docs or `--help`:
   - How does it take a prompt: on stdin (`echo "hi" | mycli`), or as an argument (`mycli -p "hi"`)?
   - Which flags make it read only, let it edit files, and let it run commands?
   - How do you choose a model?
   - Does it print plain text, or JSON lines? Can it continue an earlier session?
3. **Try it by hand** in a project folder, with a harmless prompt. See what it prints, and check that it ends
   by itself.

## Adding it

1. Open the runner's settings file: `~/.bullpen/runner.json` (on Windows,
   `%USERPROFILE%\.bullpen\runner.json`).
2. Add an entry under `"agents"`. The name (here `mycli`) is what the dashboard shows. This example is for a
   made-up program that reads the prompt on stdin:

   ```json
   "mycli": {
     "preset": "text",
     "label": "My CLI",
     "command": "mycli",
     "args": ["--print", "--no-color"],
     "autonomy": {
       "readonly": ["--read-only"],
       "edits": ["--allow-edits"],
       "full": ["--allow-edits", "--allow-commands"]
     },
     "model": ["--model", "{model}"],
     "models": ["small", "large"]
   }
   ```

3. Save the file. A running runner picks up the change within 15 seconds, so you don't need to restart it.
4. Test it from the machine, without the hub:

   ```bash
   python3 ~/.bullpen/runner/runner.py test mycli
   ```

   (On Windows: `py -3 $HOME\.bullpen\runner\runner.py test mycli`.)
5. On the dashboard, send it a **Read only** task in the `chat` project first.

> **Read only is only as safe as the flags you give.** For a text agent, "Read only" means "run it with the
> `readonly` flags". If the program has no read-only mode, a read-only job runs it with its normal
> permissions, which may include editing files. Give such an agent only projects where that's acceptable.

## Every setting an agent entry can have

| Key | What it does |
|---|---|
| `preset` | `text` for any program. (`claude`, `gemini` and `selftest` are the built-in ones; the setup writes those for you.) |
| `label` | the name shown on the dashboard |
| `command` | the program, by name (found on PATH) or as a full path |
| `args` | arguments every job gets |
| `autonomy` | the flags for each level: `readonly`, `edits`, `full`, and `ask` (edit files, ask the owner before commands) for programs that can ask through the hub's `approve` job tool, as Claude Code does with `--permission-prompt-tool mcp__bullpenjob__approve`. An empty list means no flags. A level you give replaces the preset's flags for that level only (the `text` preset's are empty), and a level set to `null` removes it. A job that asks for a level the agent doesn't have fails with "<agent> has no '<level>' level in its settings." |
| `model` | the flags used when a task names a model, with `{model}` where the model goes |
| `models` | the model suggestions the dashboard offers |
| `model_name` | the model used when the task leaves the box blank |
| `resume` | the flags that continue an earlier conversation, with `{session_id}` |
| `new_session` | the flags for a new conversation, with `{new_session_id}`: a fresh id the runner makes up and reports as the session id, so a follow-up can pass it to `resume`. For programs that let you choose the session id. |
| `prompt_via` | `stdin` (the default) writes the prompt to the program's input. `arg` passes it as an argument: in place of `{prompt}` if one of the arguments contains it, otherwise as the last argument. |
| `env` | environment variables for every job, for example `{"MYCLI_HOME": "~/.mycli-work"}` (a leading `~` is expanded) |
| `autonomy_env` | extra environment variables per level, for example `{"full": {"MYCLI_MODE": "auto"}}`, for programs that take their permission mode from the environment |
| `unset_env` | environment variables to remove before the program starts |
| `extra_args` | arguments added after all the others (only a prompt passed as the last argument comes later) |
| `parser` | how the output is read: `text` (the default for this preset), or `claude-stream` / `gemini-stream` for programs whose JSON lines look exactly like Claude Code's or Gemini CLI's |
| `terminal` | the command that continues a session in your own terminal, with `{session_id}`, for **Continue in a terminal** on the job's page. `""` shows none. The built-in presets work it out for you. |
| `job_mcp` | how the agent is handed the hub's [job tools](02-architecture.md#job-tools-what-a-running-agent-can-ask-the-hub), for jobs that get them. `mcp-config`: a file of MCP settings for that job, passed with `mcp_config_args`; it gives the job tools a timeout a minute longer than an approval may wait, and has them loaded at start. `gemini-settings` (the `gemini` preset): a copy of the machine's Gemini CLI `system-defaults.json` for that job only, with the job tools added, passed in the environment variable `GEMINI_CLI_SYSTEM_DEFAULTS_PATH`; the command line stays the same, and your own, the project's and an administrator's Gemini settings still win over it. Gemini CLI 0.60 and later read that file only when root owns it and every folder above it (on Windows, an administrator or SYSTEM), so this works only for a runner that runs as root; elsewhere the runner doesn't report `job_mcp` for the agent, and Gemini agents can take subtasks but not hand them out ([guide 9](09-troubleshooting.md#gemini-cli)). Leave it out and the agent never gets them. |
| `mcp_config_args` | the flags that pass that file, with `{job_mcp_config}` where its path goes, for example `["--mcp-config", "{job_mcp_config}"]` |
| `allow_tools_flag` | the flag that allows tools by name, for example `--allowedTools`. For a job that gets the job tools, the runner adds their names right after this flag in the level's flags, or adds the flag and the names after the level's flags when the flag isn't there. |
| `live` | the flags that keep the program's input open for more messages, one JSON line each in Claude Code's stream-json format, for "Keep the session open for messages": `["--input-format", "stream-json"]` for Claude Code. Only for `claude-stream` programs that take the prompt on `stdin`. Leave it out and the agent answers once per job. |
| `remote_control` | the arguments (after `command`) that start the program's Remote Control server in a project, with `{rc_name}` and `{rc_spawn}`: `["remote-control", "--name", "{rc_name}", "--spawn", "{rc_spawn}"]` for Claude Code. Used only for agents with a full claude.ai login, on machines with `allow_remote_control` ([guide 5](05-linux-and-macos-machines.md#remote-control)). |
| `account` | the account label: a word for the account the agent is signed in to, the same on every machine signed in to it. The Usage page joins the plan limits of agents with the same label ([guide 5](05-linux-and-macos-machines.md#account-labels)). Leave it out and the agent is an account of its own. |
| `usage_check` | how **Check limits** asks a Claude Code agent for its plan limits: `{"args": [...], "prompt": "/usage"}`. The runner starts `command` with these `args` only (not the agent's `args`, levels or `extra_args`), in the `chat` folder, writes the prompt to it and reads Claude Code's JSON lines. The `claude` preset has it; leave it out for any other program. |

**A machine setting about agents.** `credits_agent`, at the top level of `runner.json` (not in an agent's
entry), names the agent the hub may use for background work paid by your plan's monthly API credits. Only a
Claude agent (`claude` or `selftest` preset) that signs in with an API key counts; a text agent never does, and
the runner ignores the setting with a line in its log
([guide 5, A credits agent](05-linux-and-macos-machines.md#a-credits-agent)).

**Templates** you can use in any list of arguments: `{model}`, `{session_id}`, `{new_session_id}`,
`{workdir}` (the project folder, for programs that want it as a flag such as `--dir {workdir}`),
`{job_mcp_config}` (in `mcp_config_args` only), and `{rc_name}` and `{rc_spawn}` (in `remote_control`
only: the server's name, such as `build-box myapp`, and `worktree` in a git project or `same-dir`
otherwise). Every job also gets `PWD` set to its project folder, because
some programs read the folder from there.

**The order** of the command line is:

1. `command`, then `args`;
2. `live` (only for a task with "Keep the session open for messages");
3. the level's `autonomy` flags (with the job tools' names after `allow_tools_flag`, for a job that gets them);
4. `mcp_config_args` (only for a job that gets the job tools);
5. `model` (when a model is chosen);
6. `resume` (on a follow-up) or `new_session` (otherwise);
7. `extra_args`;
8. the prompt, when `prompt_via` is `arg` and no argument holds `{prompt}`.

So flags in `extra_args` come after the `resume` ones; for programs where that matters, such as
`mycli resume <id> --flag`, put the flag in `args` instead. A job without job tools has the same command line
as in earlier versions.

**Your own levels and permission flags.** If you give an agent its own `autonomy` or `autonomy_env`, the
runner no longer tells the hub that its level names mean what they say (the `levels` feature below). The
same goes for a flag that changes permissions anywhere else in the command line: in `args`, `extra_args`,
`live`, `model`, `resume`, `new_session` or `mcp_config_args`, or a different `allow_tools_flag`
(`--allowed-tools` is the same flag as the preset's `--allowedTools`, so it doesn't count). A later
`--permission-mode` wins over the level's, so `"extra_args": ["--permission-mode", "auto"]` would run a
read-only job in auto mode. For Claude Code these flags are `--permission-mode`,
`--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`, `--allowedTools`,
`--disallowedTools`, `--permission-prompt-tool`, `--permission-prompts`, `--settings`,
`--setting-sources`, `--add-dir`, `--tools`, `--agent`, `--agents`, `--plugin-dir` and `--plugin-url`
([CLI reference](https://code.claude.com/docs/en/cli-reference), last checked 7 November 2026). For Gemini
CLI they are `--approval-mode`, `--yolo` (`-y`), `--allowed-tools`, `--policy`, `--admin-policy` and
`--include-directories` ([its options](https://github.com/google-gemini/gemini-cli/blob/main/packages/cli/src/config/config.ts),
last checked 7 November 2026). The agent still runs your own tasks as before, but the hub won't send it
subtasks, and the runner refuses one that is already on its way: it couldn't be sure a "read only" subtask
only reads. A permission flag also takes away **Edit files, ask me before commands** (the `approvals`
feature), because it would win over asking too; the runner refuses such a job that is already on its way.
The machine's check-in names the flags, so the dashboard and the hub's refusals say which one it is. To
change what a level does, change that level's flags in `autonomy` instead.

**Prompts as arguments** have limits on Windows: a command line can hold about 32,000 characters, and a
`.cmd` or `.bat` launcher can't be given a prompt safely (cmd.exe would read characters such as `&` and `|`
in it as commands). The runner refuses both cases with a clear message. Point `command` at the real program
(often a `.exe` or `node.exe` with the script) or use `stdin`.

**Secrets.** Put API keys in `env`, never in `args`. The runner writes each command line into its log (with
known key formats replaced by `***`), and other users on the machine can see command lines in the process
list.

## An example: Qwen Code

[Qwen Code](https://github.com/QwenLM/qwen-code) (Apache-2.0) reads the prompt on stdin and prints JSON lines
close to Claude Code's, so the `claude-stream` parser may be enough for a live log and follow-ups. This entry
comes from its documentation and hasn't been tested with Agent Bullpen yet:

```json
"qwen": {
  "preset": "text",
  "label": "Qwen Code",
  "command": "qwen",
  "args": ["--output-format", "stream-json"],
  "parser": "claude-stream",
  "autonomy": {
    "readonly": ["--approval-mode", "plan"],
    "edits": ["--approval-mode", "auto-edit"],
    "full": ["--approval-mode", "yolo"]
  },
  "resume": ["--resume", "{session_id}"],
  "model": ["--model", "{model}"]
}
```

If you try it, check two things on a real run and please report what you find in an issue. Does `--resume`
with the prompt on stdin stay headless? How does a "plan" (read-only) job end?

## What full support needs

Full support for a new agent is code in the runner and the hub. Today it touches about a dozen files, roughly
300 to 500 lines:

1. **A preset** in `PRESETS` in `runner/runner.py`, like the entry above.
2. **An output parser** that turns the program's JSON lines into readable log lines, and finds the session id,
   the final answer and errors.
3. **A readiness check**: is it installed, and does it look signed in?
4. **Setup**: detecting it in `runner.py init`, and installing it in `scripts/linux/setup-workstation.sh` and
   `scripts/windows/Setup-Workstation.ps1`.
5. **Settings wiring** (the `Wiring` class in `runner/runner.py`): pinned notes into the program's own memory
   file, the hub's `bullpen` server and the central MCP servers into its MCP settings, and the add-ons into
   its skills folder. The hub's list of agents that MCP servers can go to (`TARGETS` in `hub/registry.py`) and
   the MCP page need the new agent too.
6. **Features** the runner reports for it in each check-in, so the hub only sends jobs it can do
   (`agent_features` in `runner/runner.py`):

   | Feature | What it says | What it needs |
   |---|---|---|
   | `job_mcp` | the agent can be handed the hub's job tools, so it can hand out subtasks in tasks where you allow it | a `job_mcp` way to pass them: `mcp-config` with `mcp_config_args`, or `gemini-settings` for Gemini CLI on a runner that runs as root |
   | `approvals` | it can ask the owner on the dashboard before commands (the **Edit files, ask me before commands** level) | `job_mcp`, an `ask` level whose flags make the program ask through the `approve` job tool, and no flag that changes permissions elsewhere in its command line |
   | `live` | it can keep a session open for messages typed on the job's page | Claude Code's JSON lines (`claude-stream` parser), the prompt on `stdin`, and `live` flags |
   | `levels` | its `readonly`, `edits` and `full` flags are the runner's own, so the names mean what they say. Only such agents take subtasks | a built-in preset whose levels weren't replaced in `runner.json`, with no flag that changes permissions elsewhere in its command line |
   | `limits` | its jobs report the account's plan limits, which the Usage page shows | Claude Code's JSON lines (`claude-stream` parser) and a claude.ai sign-in: a login or a `claude setup-token` token, not an API key |
   | `usage_check` | **Check limits** can ask it for its plan limits | as `limits`, with a full login (not a `setup-token` token), and `usage_check` settings |
   | `remote_control` | **Start Remote Control** can run its Remote Control server | Claude Code's JSON lines, a full claude.ai login (not an API key, a `setup-token` token or another provider), and `remote_control` arguments |

   A runner too old to report features gets none, and the hub tells you to update it when a job needs one.
7. **Tests and docs**: a parser test or a fake mode in `tests/smoke_test.py`, a case in
   `tests/test_runner_units.py`, and a row in the guides.

The [roadmap](../ROADMAP.md) has the plan to make this one small file per agent, and the order in which agents
are planned. If you want to add an agent, open an issue first ([CONTRIBUTING.md](../CONTRIBUTING.md)).
