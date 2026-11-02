# c3tl

`c3tl` (Claude Code ctl) is a small command-line client for Claude Code background sessions. It starts, monitors, steers, queues, and stops them, and runs Claude Code's `/code-review`. It speaks the same JSON protocol as [codexctl](https://github.com/samuelhe52/codexctl), so Codex, opencode, and other agent harnesses can hand work to Claude Code the way they hand it to Codex. It also ships an agent skill.

Sessions are ordinary `claude --bg` sessions. They appear in `claude agents`, and a person can take one over at any point with `claude attach <shortId>`, or with `claude --resume <threadId>` once it is stopped.

It drives the `claude` CLI and reads session transcripts. It has no dependencies beyond Python 3.9+.

## Requirements

- Claude Code with background sessions (tested with `2.1.316` on macOS)
- Python 3.9 or later
- A trusted working directory: run `claude` there once and accept the trust prompt

## Install

```sh
git clone https://github.com/samuelhe52/c3tl.git
cd c3tl
./install.sh
```

The installer symlinks `c3tl` into `~/.local/bin`. Set `C3TL_BIN_DIR` to change that location.

### Agent skill

The bundled [c3tl skill](skills/c3tl/SKILL.md) teaches agents how to start and supervise sessions, run reviews, and preserve the caller's constraints. `install.sh` doesn't install it; link it into each agent that should use it. Run these commands from the repository root after installing the CLI.

Codex, opencode, and other agents that read `~/.agents/skills`:

```sh
mkdir -p ~/.agents/skills
ln -s "$PWD/skills/c3tl" ~/.agents/skills/c3tl
```

Codex with its own skills directory:

```sh
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
ln -s "$PWD/skills/c3tl" "${CODEX_HOME:-$HOME/.codex}/skills/c3tl"
```

Restart the agent to discover the skill.

## Usage

Each command prints one JSON object. Errors print `{"error": "..."}` and exit with status 1.

```sh
c3tl models                                   # model aliases, efforts, service tiers
c3tl start "Fix the flaky test" --model opus --effort high --tier priority --name flaky
c3tl start "Run the eval" --model sonnet --wait 3600   # block, then print a compact result
c3tl wait <threadId>                          # block until the session settles; compact result
c3tl watch <threadId> --seconds 120           # events until the session settles or time runs out
c3tl status <threadId>                        # session state and latest transcript items
c3tl result <threadId>                        # full latest agent message
c3tl steer <threadId> "Focus on the loss code" # interrupt the running turn and continue with this
c3tl queue <threadId> "Now write the fix"     # waits for the session to settle, then starts a new turn
c3tl stop <threadId> --kill-processes
c3tl list
c3tl archive <threadId>
c3tl review --base main --level high          # /code-review; waits and prints it
```

`<threadId>` is the full session id. The short id, a unique prefix, or the session name also work.

`review` starts a session whose prompt is `/code-review [level] [target]`. The target is uncommitted changes by default, or pass one of `--base BRANCH`, `--commit SHA`, or `--instructions TEXT`. `--level` (`low` to `max`) sets the review depth. Review accepts the same model, effort, tier, permission, cwd, name, and `-c` options as `start`, and it requests tier `default` (standard speed) unless you pass `--tier priority`. Claude Code runs the review as a background agent. When that agent finishes, `c3tl` collects its output and stops the session, so the parent model doesn't start acting on the findings. The text is in `review` (`(none)` means no findings), and structured findings, if the reviewer reported any, are in `findings`. If `--wait` (default 3600) expires, the review keeps running; use `c3tl wait`, then `c3tl result`.

`wait` blocks until the session settles, then prints a compact result: turn status, the final agent message, the command count, the number of failed commands with the last five of them (output clipped), and the changed files. "Settles" means the session is no longer busy and has no background tasks left. Claude can end a turn while its background tasks still run, and it starts a new turn when they finish. If the wait covers several turns, they're summarized together and listed in `turnIds`. If the session is already idle, `wait` returns the last turn's result at once. `start --wait SECONDS` does the same right after starting. Sessions keep running if `c3tl` exits or times out, so it's safe to run `wait` again.

### Cheap supervision from an agent

Waiting doesn't need a model. Run `c3tl start ... --wait 7200` (or `c3tl wait <threadId>`) as a background shell job, and the harness is notified when it exits. No tokens are spent while Claude works, and only the compact result comes back.

`start` options:

| Option | Meaning |
| --- | --- |
| `--model` | Model alias (`fable`, `opus`, `sonnet`, `haiku`) or full model id |
| `--effort` | `low`, `medium`, `high`, `xhigh`, or `max` |
| `--tier` | `priority` for Fast mode, `default` for standard speed |
| `--permission-mode` | `bypassPermissions` (default), `acceptEdits`, `auto`, `manual`, `dontAsk`, or `plan` |
| `--cwd` | Working directory for the session (default: current directory); must be trusted |
| `--name` | Session name shown in `claude agents` and `/resume` |
| `-c KEY=VALUE` | Claude Code setting, passed through `--settings`; dotted keys nest, and `VALUE` is parsed as JSON, falling back to a plain string |
| `--wait SECONDS` | Block until the session settles, up to SECONDS, and print the compact result |
| `-` as prompt | Read the prompt from stdin (also works for the `steer` and `queue` message); blank text is rejected |

## How it differs from codexctl

Claude Code has no app-server API, so `c3tl` builds on the background-session commands of the `claude` CLI. The command set and JSON shapes follow codexctl. These are the differences:

- **Steering interrupts.** Claude Code can't add input to a running turn. `steer` stops the session and continues it with your message. The conversation so far is kept, but the step that was in flight is lost. Use `queue` when the current direction is fine.
- **Launch options are fixed.** Claude Code saves each background session's flags and reapplies them on every continuation, so permissions stay pinned without a state file. If you pass different flags when continuing, Claude Code forks a copy instead, so `queue` and `steer` take no model, effort, tier, or permission options. A person can still change them after `claude attach`, with `/model` or `/permissions`.
- **Permissions.** Instead of `--sandbox` and `--ask-for-approval`, sessions take Claude Code's `--permission-mode`. The default is `bypassPermissions`, so Claude can change files, git state, and remote hosts without asking.
- **People can answer blocked sessions.** A session waiting on a person, such as for a permission prompt, reports `threadStatus` `blocked`, and `wait` returns it as a `serverRequest` error with `needs` describing the request. `c3tl` can't answer it, but a person can, with `claude attach`.
- **Live events are per message.** Assistant text reaches the transcript only when each message completes, so `watch` reports completed messages and tool calls, not token streams.
- **Trust.** Claude Code refuses to start in an untrusted directory, and trust isn't inherited from parent directories.

## Behavior to know

- **Stopping doesn't end commands.** `stop` stops the session, interrupting any active turn, but processes it already started keep running. `stop --kill-processes` also sends SIGTERM to the session's descendant processes. The session stays resumable either way.
- **Archive removes the session from `claude agents`.** It also ends the session's processes. The transcript is kept, so `claude --resume <threadId>` still works, but `claude attach` won't find it.
- **State sources.** Liveness comes from `claude agents --json`, turns from the transcript in `~/.claude/projects`, and background-task counts and `needs` from the undocumented `~/.claude/jobs/<shortId>/state.json`. If that file's format changes, those two fields degrade, but `wait` still ends when the session goes idle. Set `C3TL_CLAUDE` to use another `claude` executable, and `CLAUDE_CONFIG_DIR` to use another data directory.

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests use a fake `claude` CLI and don't need Claude Code installed.

## License

MIT
