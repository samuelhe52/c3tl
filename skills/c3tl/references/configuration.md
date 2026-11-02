# Configuration and troubleshooting

## Requirements and failures

The CLI requires Python 3.9+ and Claude Code with background sessions (`claude --bg`, `claude agents`). `C3TL_CLAUDE` overrides the `claude` executable and `CLAUDE_CONFIG_DIR` the Claude Code data directory (default `~/.claude`).

- `Workspace not trusted`: Claude Code has not trusted the `--cwd` directory. Trust is per directory and not inherited from a parent. A person must run `claude` there once and accept the prompt; do not edit trust settings yourself.
- `started a copy`: `claude --bg --resume` forked the session instead of continuing it. `c3tl` reports this as an error; the copy is a separate session that you may need to `archive`.

## Overrides and persistence

Repeated `-c KEY=VALUE` options become a `--settings` object for the session. Dotted keys nest, and values are parsed as JSON, falling back to strings. `--tier priority` sets `fastMode: true`; `--tier default` sets it to false.

Claude Code saves each background session's launch flags (model, effort, permission mode, settings, name) and reapplies them whenever the session continues, including turns started by `queue` and `steer`. Passing different flags when continuing would fork a copy, so the launch options can't change. `status` reports them as `launchFlags`. A person who resumes interactively with `claude --resume` gets their own defaults instead.

## How state is read

- Liveness and busy/idle/waiting status come from `claude agents --json --all`.
- Turns and items come from the session transcript, `~/.claude/projects/*/THREAD_ID.jsonl`. Each new prompt id starts a turn: a prompt, a slash command, or a background-task notification.
- Background-task counts and blocked-on-a-person details (`needs`) come from `~/.claude/jobs/SHORT_ID/state.json`. That file is undocumented; if Claude Code changes it, `waitingOnTasks` and `needs` degrade, but `wait` still ends when the session goes idle.
- Assistant text reaches the transcript only when each message completes, so `watch` shows completed messages and tool calls, not token streams.

## Process cleanup

`stop --kill-processes` and `archive` send SIGTERM to the processes descended from the session's process before stopping it. Processes that had already detached, and remote jobs, may survive.
