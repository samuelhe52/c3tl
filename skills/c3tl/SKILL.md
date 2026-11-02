---
name: c3tl
description: Start, inspect, steer, and stop Claude Code background sessions through the local c3tl CLI, or run Claude Code's /code-review. Use when the user asks to delegate work to Claude Code or manage c3tl sessions.
---

# c3tl

Operate Claude Code background sessions with `c3tl`. Commands return one JSON object; failures return `error` and exit 1. Consult subcommand `--help` for options and `c3tl models` for model aliases and effort levels.

Each session is an ordinary Claude Code background session. A person can open it with `claude attach SHORT_ID` (both returned by `start`) or, once stopped, `claude --resume THREAD_ID`. Don't archive a session the user may want to open.

## Prepare and start

- Pass only the model, effort, tier, permission mode, and setting flags the user requested. Omit every other flag so the CLI defaults apply, even when a different setting seems better for the task. `start` and `review` default to `--permission-mode bypassPermissions`; never widen an explicitly requested permission mode to work around a failure.
- A session keeps its launch options for its whole life. `queue` and `steer` cannot change the model, effort, tier, or permission mode; start a new session if those must change.
- Set `--cwd` to the intended repository. Claude Code must already trust that directory; a trust error means a person must run `claude` there once and accept the prompt. Put the user's constraints verbatim at the top of the prompt. Delegation grants no additional authorization for edits, commits, pushes, or remote actions.
- Only modify sessions the user identified or that you started for this task. Resolve ambiguous targets with `list`.

Use a quoted heredoc for substantial prompts; `start`, `queue`, and `steer` read stdin when the prompt or message is `-`:

```sh
c3tl start - --cwd /path/to/repo --name scoped-fix <<'PROMPT'
Constraints: Only edit the requested files. Do not commit, push, or modify remote state.
Task: Implement the requested fix and run the relevant checks. Report changed files and validation results.
PROMPT
```

Save the returned `threadId`. `start` returns immediately; `--wait SECONDS` waits for a compact result. Use background execution for long waits to keep the supervising conversation responsive.

## Supervise and collect results

| Intent | Command | Behavior |
| --- | --- | --- |
| Locate sessions | `c3tl list --limit 10` | Returns IDs, names, cwd, and status. |
| Read saved progress | `c3tl status THREAD_ID` | Reads the transcript, including the turn in progress. |
| Wait for completion | `c3tl wait THREAD_ID --seconds 60` | Returns the last result immediately if idle. |
| See live progress | `c3tl watch THREAD_ID --seconds 30` | Collects events into one response, not a continuous stream. |
| Read latest reply | `c3tl result THREAD_ID` | Does not prove the turn ended. |
| Redirect active work | `c3tl steer THREAD_ID "Focus on the failing check"` | Interrupts the running turn, then continues with the message. |
| Start a follow-up | `c3tl queue THREAD_ID "Run the remaining checks" --wait 60` | Waits for the session to settle, then starts the next turn; does not wait for it to finish. |

Use `wait` for completion; do not poll with `watch`. Reserve `watch` for mid-course attention or requested live progress.

- `THREAD_ID` accepts the full id, the short id, a unique prefix, or the session name.
- Confirm a hand-off from the returned JSON (`turnId`, `queued`, or `steered`) before reporting that work has started.
- If `finished` is false, the session keeps running. Wait again on the same thread. Check `turnStatus` and errors before claiming success; finished turns can be `interrupted`.
- `threadStatus` `waitingOnTasks` means Claude ended its turn but has background tasks running; it starts another turn when they finish, and `wait` keeps waiting for that.
- A `serverRequest` error (`threadStatus` `blocked`) means the session waits on a person, such as a permission prompt; `needs` says what. `c3tl` cannot answer it. Report the blockage; waiting will not resolve it.
- `steer` cannot add to a running turn: it stops the session and continues it with your message, so the interrupted turn's partial work stays but its in-flight step is lost. Prefer `queue` unless the current direction is wrong.
- After a queue timeout or ambiguous submission failure, inspect the session or list sessions before retrying to avoid duplicate work.

Report the thread ID, outcome, validation, and remaining work.

## Built-in review

```sh
c3tl review --cwd /path/to/repo --base main --wait 3600
```

The default target is uncommitted changes. Choose at most one of `--base BRANCH`, `--commit SHA`, or `--instructions TEXT`. `--level` sets the review depth (`low` to `max`); omit it unless the user asked for one, and use `max` only when they explicitly request it. Review uses tier `default` (standard speed) unless explicitly overridden; `--tier priority` selects Fast mode.

`/code-review` has its own instructions; don't restate or override them. Prefer a built-in target and pass no text. Use `--instructions` only when no built-in target fits, with minimal text that names the target, plus the user's specific requests verbatim if they made any.

The review runs in a background agent. `c3tl` stops the session once the reviewer finishes so the parent model doesn't act on the findings. Completed review text is in `review` (`(none)` means no findings) and structured findings, when reported, in `findings`. If `--wait` expires, the review keeps running: `c3tl wait`, then `c3tl result`.

## Stop sessions

- `stop THREAD_ID` stops the session and interrupts any active turn; commands it started may keep running. The session stays resumable.
- `stop THREAD_ID --kill-processes` also ends the processes the session started.
- `archive THREAD_ID` removes the session from `claude agents` and ends its processes; the transcript stays, so `claude --resume` still works. It is not harmless history cleanup: `claude attach` no longer finds it.

Only stop processes within the requested scope. Detached or remote jobs may survive.

For the claude CLI location, settings overrides, and how state is read, see [configuration and troubleshooting](references/configuration.md).
