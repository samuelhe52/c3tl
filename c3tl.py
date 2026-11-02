#!/usr/bin/env python3
"""Start, monitor, steer, queue, and stop Claude Code background sessions.

Drives `claude --bg` sessions through the claude CLI and reads their transcripts. Standard library only.
Every command prints one JSON object on stdout; failures print {"error": ...} and exit 1.
Sessions are ordinary Claude Code background sessions: a person can open one at any time with
`claude attach <shortId>`, or with `claude --resume <threadId>` once it is stopped.
"""
import argparse
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time

PERMISSION_MODES = ["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"]
DEFAULT_PERMISSION_MODE = "bypassPermissions"
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
MODEL_ALIASES = ["fable", "opus", "sonnet", "haiku"]
REVIEW_LEVELS = ["low", "medium", "high", "xhigh", "max"]
FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
POLL = float(os.environ.get("C3TL_POLL", "2"))


class C3Error(Exception):
    pass


# ---------------------------------------------------------------- claude CLI and on-disk state

def claude_bin():
    return os.environ.get("C3TL_CLAUDE") or "claude"


def claude_home():
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def run_claude(args, cwd=None, timeout=120):
    try:
        p = subprocess.run([claude_bin(), *args], cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise C3Error(f"claude CLI not found ({claude_bin()}); install Claude Code or set C3TL_CLAUDE") from e
    except subprocess.TimeoutExpired as e:
        raise C3Error(f"`claude {args[0]}` did not finish within {timeout}s") from e
    if p.returncode != 0:
        msg = (p.stderr.strip() or p.stdout.strip() or f"exit {p.returncode}")
        raise C3Error(f"claude {args[0]}: {msg}")
    return p.stdout


def agents():
    """Every session `claude agents` knows about, running or not."""
    out = run_claude(["agents", "--json", "--all"])
    try:
        return json.loads(out)
    except ValueError as e:
        raise C3Error(f"could not parse `claude agents --json` output: {out[:200]!r}") from e


def job_state(short):
    if not short:
        return None
    try:
        with open(os.path.join(claude_home(), "jobs", short, "state.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def transcript_path(sid):
    hits = glob.glob(os.path.join(claude_home(), "projects", "*", f"{sid}.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def resolve(ref):
    """Find a session by full id, short id, unique id prefix, or exact name."""
    ref = ref.strip()
    found = agents()
    for key in ("sessionId", "id", "name"):
        hits = [a for a in found if a.get(key) == ref]
        if len(hits) == 1:
            return hits[0]
    hits = [a for a in found if (a.get("sessionId") or "").startswith(ref)]
    if len(hits) > 1:
        raise C3Error(f"{ref!r} matches {len(hits)} sessions; pass more of the id")
    if hits:
        return hits[0]
    # Not a background session (or already removed): fall back to its transcript.
    files = glob.glob(os.path.join(claude_home(), "projects", "*", f"{glob.escape(ref)}*.jsonl"))
    sids = sorted({os.path.basename(f)[:-6] for f in files})
    if len(sids) > 1:
        raise C3Error(f"{ref!r} matches {len(sids)} transcripts; pass more of the id")
    if sids:
        return {"sessionId": sids[0], "kind": "transcript"}
    raise C3Error(f"no session matches {ref!r}; see `c3tl list`")


def activity(sess, job=None):
    """Codex-style thread status: active, waitingOnTasks, blocked, idle, or notLoaded."""
    if "pid" not in sess:
        return "notLoaded"
    if sess.get("status") == "waiting" or sess.get("state") == "blocked":
        return "blocked"
    if sess.get("status") == "busy":
        return "active"
    # `inFlight.queued` can stay at 1 on an idle session, so only running tasks count.
    if ((job or {}).get("inFlight") or {}).get("tasks"):
        return "waitingOnTasks"
    return "idle"


def refresh(sess):
    for a in agents():
        if a.get("sessionId") == sess["sessionId"]:
            return a
    return {"sessionId": sess["sessionId"], "kind": "transcript"}


def blocker(sess, job):
    need = (job or {}).get("needs") or sess.get("waitingFor") or "a person"
    return {"event": "serverRequest", "needs": need,
            "note": "the session is waiting on a person; c3tl cannot answer it. "
                    f"Open it with `claude attach {sess.get('id') or sess['sessionId']}`"}


# ---------------------------------------------------------------- transcript parsing

def clip(v, limit):
    return v[-limit:] if isinstance(v, str) and len(v) > limit else v


def text_of(content):
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text", ""))
        elif isinstance(c, str):
            parts.append(c)
    return "\n".join(parts)


def tool_item(block):
    name, inp = block.get("name") or "", block.get("input") or {}
    if name == "Bash":
        return {"type": "commandExecution", "command": inp.get("command"), "status": "inProgress"}
    if name in FILE_TOOLS:
        return {"type": "fileChange", "status": "inProgress",
                "paths": [p for p in (inp.get("file_path"), inp.get("notebook_path")) if p]}
    if name.startswith("mcp__"):
        _, server, tool = (name.split("__", 2) + ["", ""])[:3]
        return {"type": "mcpToolCall", "server": server, "tool": tool, "status": "inProgress"}
    item = {"type": "toolCall", "tool": name, "status": "inProgress"}
    for k in ("description", "pattern", "query", "url", "file_path", "subagent_type"):
        if inp.get(k):
            item[k] = inp[k]
    return item


class Transcript:
    """Incremental parser of a session transcript into Codex-style turns and items.

    A turn starts at each new promptId on the main thread (a typed prompt, a slash command, or a
    task notification) and ends at the matching `turn_duration` (or `local_command`) record.
    """

    def __init__(self, path):
        self.path, self.offset = path, 0
        self.turns, self.tools, self.cwd = [], {}, None

    def read(self):
        """Parse records appended since the last read; returns the events they produce."""
        events = []
        if not self.path:
            return events
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return events
        end = data.rfind(b"\n") + 1  # leave a partially written last line for the next read
        for line in data[:end].splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            events.extend(self.feed(e))
        self.offset += end
        return events

    @property
    def current(self):
        return self.turns[-1] if self.turns else None

    def _new_turn(self, tid, origin):
        self.turns.append({"id": tid, "origin": origin, "status": "inProgress", "items": []})
        return {"event": "turnStarted", "turnId": tid, "origin": origin}

    def _add(self, item):
        if not self.turns:
            self._new_turn(None, "unknown")
        self.turns[-1]["items"].append(item)

    def feed(self, e):
        if e.get("isSidechain"):
            return []
        t, msg = e.get("type"), e.get("message") or {}
        self.cwd = self.cwd or e.get("cwd")
        if t == "user":
            content = msg.get("content")
            results = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_result"] \
                if isinstance(content, list) else []
            if results:
                return [ev for r in results for ev in self._result(r)]
            events = []
            tid = e.get("promptId") or e.get("uuid")
            text = text_of(content)
            if not self.turns or self.turns[-1]["id"] != tid:
                events.append(self._new_turn(tid, None))
            turn = self.turns[-1]
            if not e.get("isMeta"):
                if turn["origin"] is None:  # the first visible message says what started the turn
                    turn["origin"] = ("taskNotification" if text.lstrip().startswith("<task-notification>")
                                      else "command" if text.lstrip().startswith("/") else "user")
                    if events:
                        events[-1]["origin"] = turn["origin"]
                item = {"type": "userMessage", "text": text}
                self._add(item)
                events.append({"event": "completed", **item})
            return events
        if t == "assistant":
            events = []
            if e.get("isApiErrorMessage") or e.get("error"):
                item = {"type": "error", "text": text_of(msg.get("content")) or str(e.get("error"))}
                self._add(item)
                return [{"event": "error", "message": item["text"]}]
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    item = {"type": "agentMessage", "text": block["text"]}
                    self._add(item)
                    events.append({"event": "completed", **item})
                elif block.get("type") == "tool_use":
                    item = tool_item(block)
                    self.tools[block.get("id")] = item
                    self._add(item)
                    events.append({"event": "started", **{k: v for k, v in item.items() if k != "status"}})
            return events
        if t == "system" and e.get("subtype") in ("turn_duration", "local_command"):
            turn = self.current
            if turn and turn["status"] == "inProgress":
                turn["status"] = "completed"
                if e.get("durationMs") is not None:
                    turn["durationMs"] = e["durationMs"]
                return [{"event": "turnCompleted", "turnId": turn["id"], "status": "completed"}]
        return []

    def _result(self, r):
        item = self.tools.pop(r.get("tool_use_id"), None)
        if item is None:
            return []
        out = text_of(r.get("content"))
        failed = bool(r.get("is_error"))
        item["status"] = "failed" if failed else "completed"
        if item["type"] == "commandExecution":
            m = re.match(r"\s*(?:Error: )?Exit code (\d+)", out or "")
            item["exitCode"] = int(m.group(1)) if m else (1 if failed else 0)
            item["aggregatedOutput"] = out
        elif failed:
            item["error"] = out
        return [{"event": "completed", **item}]

    def settle(self, live_state):
        """Mark an unfinished last turn interrupted when the session is no longer working on it."""
        turn = self.current
        if turn and turn["status"] == "inProgress" and live_state not in ("active", "blocked"):
            turn["status"] = "interrupted"


def load(sess):
    tr = Transcript(transcript_path(sess["sessionId"]))
    tr.read()
    return tr


def summarize_item(item, limit):
    return {k: clip(v, limit) for k, v in item.items()}


def turn_summary(turns, chars=500, max_failed=5):
    """Compact outcome of one or more turns: final message, failed commands, changed files, errors."""
    if not turns:
        return {"turnId": None, "turnStatus": None, "text": None, "commands": 0, "failedCount": 0,
                "failedCommands": [], "filesChanged": []}
    items = [i for t in turns for i in t["items"]]
    msgs = [i["text"] for i in items if i["type"] == "agentMessage"]
    cmds = [i for i in items if i["type"] == "commandExecution"]
    failed = [i for i in cmds if i.get("exitCode") not in (None, 0)]
    files = sorted({p for i in items if i["type"] == "fileChange" and i["status"] != "failed"
                    for p in i.get("paths") or []})
    last = turns[-1]
    out = {"turnId": last["id"], "turnStatus": last["status"], "text": msgs[-1] if msgs else None,
           "commands": len(cmds), "failedCount": len(failed),
           # Exploratory commands often fail harmlessly (grep with no match), so keep only the latest few.
           "failedCommands": [{"command": clip(i.get("command"), 300), "exitCode": i.get("exitCode"),
                               "output": clip(i.get("aggregatedOutput"), chars)} for i in failed[-max_failed:]],
           "filesChanged": files}
    if len(turns) > 1:
        out["turnIds"] = [t["id"] for t in turns]
    errors = [i["text"] for i in items if i["type"] == "error"]
    if errors:
        out["errors"] = errors
    return out


# ---------------------------------------------------------------- waiting

def wait_settled(sess, seconds, tr=None, on_events=None):
    """Poll until the session stops working (idle, blocked, or gone) or `seconds` pass."""
    deadline = time.time() + seconds
    tr = tr or load(sess)
    while True:
        sess = refresh(sess)
        job = job_state(sess.get("id"))
        state = activity(sess, job)
        events = tr.read()
        if on_events and events:
            on_events(events)
        if state not in ("active", "waitingOnTasks"):
            # The process can go idle a moment before the final records reach the transcript.
            time.sleep(min(POLL, 0.5))
            events = tr.read()
            if on_events and events:
                on_events(events)
            tr.settle(state)
            return sess, job, state, tr, True
        if time.time() >= deadline:
            return sess, job, state, tr, False
        time.sleep(min(POLL, max(0.0, deadline - time.time())))


def result_of_wait(sess, job, state, tr, done, first_index, seconds):
    sid = sess["sessionId"]
    if not done:
        return {"threadId": sid, "turnId": tr.current and tr.current["id"], "finished": False,
                "threadStatus": state,
                "note": f"session still working after {seconds}s; it keeps running. "
                        f"Run `c3tl wait {sid}` again, or `watch` for live events"}
    out = {"threadId": sid, "finished": True, "threadStatus": state,
           **turn_summary(tr.turns[first_index:] or tr.turns[-1:])}
    if state == "blocked":
        out["finished"] = False
        out["errors"] = out.get("errors", []) + [blocker(sess, job)]
    return out


def wait_new_turn(sess, known, seconds=30):
    """Wait for a turn id not in `known` to appear in the transcript; returns it or None."""
    deadline = time.time() + seconds
    while True:
        path = transcript_path(sess["sessionId"])
        if path:
            tr = Transcript(path)
            tr.read()
            fresh = [t["id"] for t in tr.turns if t["id"] not in known]
            if fresh:
                return fresh[0]
        if time.time() >= deadline:
            return None
        time.sleep(min(POLL, 1))


# ---------------------------------------------------------------- launching

def message_text(arg):
    """Resolve a prompt or message argument; `-` reads stdin. Rejects blank text before anything is sent."""
    text = sys.stdin.read() if arg == "-" else arg
    if not text.strip():
        raise C3Error("empty message" + (" on stdin" if arg == "-" else ""))
    return text


def parse_value(v):
    try:
        return json.loads(v)
    except ValueError:
        return v


def settings_overrides(a):
    """`-c KEY=VALUE` and `--tier` as a Claude Code --settings object (dotted keys nest)."""
    out = {}
    for kv in a.config or []:
        if "=" not in kv:
            raise C3Error(f"-c expects key=value, got {kv!r}")
        k, v = kv.split("=", 1)
        node, parts = out, k.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
            if not isinstance(node, dict):
                raise C3Error(f"-c {k}: {p} is already set to a non-object value")
        node[parts[-1]] = parse_value(v)
    if a.tier:
        out["fastMode"] = a.tier == "priority"
    return out


def session_flags(a):
    flags = []
    if a.permission_mode:
        flags += ["--permission-mode", a.permission_mode]
    if a.model:
        flags += ["--model", a.model]
    if a.effort:
        flags += ["--effort", a.effort]
    settings = settings_overrides(a)
    if settings:
        flags += ["--settings", json.dumps(settings)]
    return flags


def launch(args, cwd):
    """Run `claude --bg ...` and return the agents entry of the session it started or woke."""
    out = run_claude(["--bg", *args], cwd=cwd)
    m = re.search(r"backgrounded\s*·\s*([0-9a-f]+)", out)
    if not m:
        raise C3Error(f"could not find the session id in `claude --bg` output: {out.strip()[:300]!r}")
    short = m.group(1)
    for _ in range(20):
        hit = [s for s in agents() if s.get("id") == short]
        if hit:
            return hit[0], out
        time.sleep(0.5)
    raise C3Error(f"session {short} started but `claude agents` does not list it")


def thread_info(sess, a, permission_mode):
    sid = sess["sessionId"]
    return {"threadId": sid, "shortId": sess.get("id"), "name": sess.get("name"),
            "model": a.model, "effort": a.effort,
            "permissionMode": permission_mode, "cwd": sess.get("cwd"),
            "attach": f"claude attach {sess.get('id')}", "resume": f"claude --resume {sid}"}


def start_session(a, prompt, name=None):
    cwd = os.path.abspath(a.cwd)
    if not os.path.isdir(cwd):
        raise C3Error(f"--cwd {cwd} is not a directory")
    args = session_flags(a)
    if name or a.name:
        args += ["--name", name or a.name]
    sess, _ = launch([*args, "--", prompt], cwd)
    return sess, wait_new_turn(sess, set())


def stop_session(sess):
    run_claude(["stop", sess.get("id") or sess["sessionId"]])
    for _ in range(20):
        if "pid" not in refresh(sess):
            return
        time.sleep(0.5)
    raise C3Error(f"session {sess.get('id')} still running after `claude stop`")


def resume_with(sess, message):
    """Continue a stopped session in the background with a new prompt, under the same id."""
    tr = load(sess)
    known = {t["id"] for t in tr.turns}
    cwd = sess.get("cwd") or tr.cwd or os.getcwd()
    # Any launch flag here would make claude start a copy; the session keeps its saved options.
    new, out = launch(["--resume", sess["sessionId"], "--", message], cwd)
    if new.get("sessionId") != sess["sessionId"]:
        raise C3Error(f"claude started a copy ({new.get('sessionId')}) instead of continuing "
                      f"{sess['sessionId']}: {out.strip()[:200]}")
    return new, wait_new_turn(new, known)


# ---------------------------------------------------------------- commands

def cmd_start(a):
    prompt = message_text(a.prompt)
    sess, turn_id = start_session(a, prompt)
    info = {**thread_info(sess, a, a.permission_mode), "turnId": turn_id}
    if a.wait is None:
        return info
    sess, job, state, tr, done = wait_settled(sess, a.wait)
    first = next((i for i, t in enumerate(tr.turns) if t["id"] == turn_id), 0)
    return {**info, **result_of_wait(sess, job, state, tr, done, first, a.wait)}


def review_output(sid, since=0.0):
    """Final text (and structured findings, if any) of the code-review skill run in this session."""
    path = transcript_path(sid)
    if not path:
        return None
    best = None
    for marker in glob.glob(os.path.join(path[:-6], "subagents", "*.forked-skill.json")):
        try:
            with open(marker) as f:
                skill = json.load(f).get("skillName")
        except (OSError, ValueError):
            continue
        log = marker[:-len(".forked-skill.json")] + ".jsonl"
        if skill != "code-review" or not os.path.exists(log) or os.path.getmtime(log) < since:
            continue
        if best is None or os.path.getmtime(log) > os.path.getmtime(best):
            best = log
    if best is None:
        return None
    try:
        with open(best[:-len(".jsonl")] + ".meta.json") as f:
            finished = bool(json.load(f).get("finished"))
    except (OSError, ValueError):
        finished = False
    text, findings = None, None
    with open(best) as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") != "assistant":
                continue
            for b in (e.get("message") or {}).get("content") or []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    text = b["text"]
                elif b.get("type") == "tool_use" and b.get("name") == "ReportFindings":
                    findings = (b.get("input") or {}).get("findings")
    return {"review": text, "findings": findings, "reviewFinished": finished}


def cmd_review(a):
    if a.base:
        target = {"type": "baseBranch", "branch": a.base}
        arg = a.base
    elif a.commit:
        target = {"type": "commit", "sha": a.commit}
        arg = f"{a.commit}^!"
    elif a.instructions:
        target = {"type": "custom", "instructions": a.instructions}
        arg = a.instructions
    else:
        target = {"type": "uncommittedChanges"}
        arg = ""
    prompt = " ".join(p for p in ("/code-review", a.level, arg) if p)
    began = time.time()
    sess, turn_id = start_session(a, prompt, name=a.name or "c3tl-review")
    sid = sess["sessionId"]
    # /code-review runs as a background agent. When it finishes, the parent session gets a task
    # notification and may start acting on the result, so stop the session as soon as the review is in.
    deadline = time.time() + a.wait
    while True:
        rv = review_output(sid, began) or {}
        sess = refresh(sess)
        job = job_state(sess.get("id"))
        state = activity(sess, job)
        if rv.get("reviewFinished") or state not in ("active", "waitingOnTasks") or time.time() >= deadline:
            break
        time.sleep(POLL)
    out = {**thread_info(sess, a, a.permission_mode), "turnId": turn_id, "target": target,
           "finished": bool(rv.get("reviewFinished")), "threadStatus": state}
    if state == "blocked":
        out["errors"] = [blocker(sess, job)]
        return out
    if not rv.get("reviewFinished"):
        if state in ("active", "waitingOnTasks"):
            out["note"] = (f"review still running after {a.wait}s; it keeps running. "
                           f"Run `c3tl wait {sid}`, then `c3tl result {sid}`")
        else:
            out["note"] = "the session ended without code-review output; see `c3tl status` for what it did"
            out["text"] = turn_summary(load(sess).turns[-1:])["text"]
        return out
    if "pid" in sess:
        stop_session(sess)
        out["threadStatus"] = "notLoaded"
    out["review"] = rv.get("review")
    if rv.get("findings") is not None:
        out["findings"] = rv["findings"]
    return out


def cmd_status(a):
    sess = resolve(a.thread)
    job = job_state(sess.get("id"))
    state = activity(sess, job)
    tr = load(sess)
    tr.settle(state)
    turns = tr.turns[-a.turns:]
    items = [(t["id"], i) for t in turns for i in t["items"]][-a.last:]
    last = tr.current or {}
    out = {"threadId": sess["sessionId"], "shortId": sess.get("id"), "name": sess.get("name"),
           "threadStatus": state, "cwd": sess.get("cwd") or tr.cwd,
           "launchFlags": (job or {}).get("respawnFlags"),
           "activeTurnId": last.get("id") if last.get("status") == "inProgress" else None,
           "lastTurn": {k: last.get(k) for k in ("id", "origin", "status", "durationMs")} if last else None,
           "recent": [dict(turn=tid, **summarize_item(i, a.chars)) for tid, i in items]}
    if state == "blocked":
        out["blocked"] = blocker(sess, job)
    if state == "waitingOnTasks":
        out["inFlight"] = (job or {}).get("inFlight")
    return out


def cmd_result(a):
    sess = resolve(a.thread)
    tr = load(sess)
    tr.settle(activity(sess, job_state(sess.get("id"))))
    out = {"threadId": sess["sessionId"], "turnId": None, "text": None}
    for t in reversed(tr.turns):
        msgs = [i for i in t["items"] if i["type"] == "agentMessage"]
        if msgs:
            out = {"threadId": sess["sessionId"], "turnId": t["id"], "turnStatus": t["status"],
                   "text": msgs[-1]["text"]}
            break
    rv = review_output(sess["sessionId"])
    if rv:
        out.update({k: v for k, v in rv.items() if v is not None})
    return out


def cmd_wait(a):
    sess = resolve(a.thread)
    tr = load(sess)
    if not tr.turns and activity(sess, job_state(sess.get("id"))) not in ("active", "waitingOnTasks"):
        return {"threadId": sess["sessionId"], "turnId": None, "finished": True, "note": "thread has no turns"}
    first = len(tr.turns) - 1 if tr.turns else 0
    sess, job, state, tr, done = wait_settled(sess, a.seconds, tr)
    return result_of_wait(sess, job, state, tr, done, max(first, 0), a.seconds)


def cmd_watch(a):
    sess = resolve(a.thread)
    state = activity(sess, job_state(sess.get("id")))
    if state not in ("active", "waitingOnTasks") and not a.wait_start:
        out = {"threadId": sess["sessionId"], "activeTurnId": None, "finished": True, "events": [],
               "threadStatus": state, "note": "no active turn; see `status` or `result`"}
        if state == "blocked":
            out["events"] = [blocker(sess, job_state(sess.get("id")))]
        return out
    tr = load(sess)
    events = []

    def collect(evs):
        for ev in evs:
            events.append({k: clip(v, a.chars) for k, v in ev.items()})

    if a.wait_start:
        # Wait for work to begin before treating an idle session as finished.
        deadline = time.time() + a.seconds
        while state not in ("active", "waitingOnTasks", "blocked") and time.time() < deadline:
            collect(tr.read())
            time.sleep(min(POLL, 1))
            sess = refresh(sess)
            state = activity(sess, job_state(sess.get("id")))
        remaining = max(0.0, deadline - time.time())
    else:
        remaining = a.seconds
    sess, job, state, tr, done = wait_settled(sess, remaining, tr, collect)
    if state == "blocked":
        events.append(blocker(sess, job))
    last = tr.current or {}
    return {"threadId": sess["sessionId"], "name": sess.get("name"), "finished": done, "threadStatus": state,
            "activeTurnId": last.get("id") if last.get("status") == "inProgress" else None,
            "droppedEvents": max(0, len(events) - a.max_events), "events": events[-a.max_events:]}


def cmd_steer(a):
    message = message_text(a.message)
    sess = resolve(a.thread)
    state = activity(sess, job_state(sess.get("id")))
    if state not in ("active", "waitingOnTasks"):
        raise C3Error(f"no active turn to steer (session is {state}); use `queue` to start a new turn")
    old = load(sess).current
    stop_session(sess)
    _, turn_id = resume_with(sess, message)
    return {"steered": True, "turnId": turn_id, "interruptedTurnId": old and old["id"],
            "note": "Claude Code cannot add input to a running turn, so c3tl interrupted it and continued the "
                    "session with this message; the conversation so far is kept. Commands the interrupted "
                    "turn started may still be running."}


def cmd_queue(a):
    message = message_text(a.message)
    sess = resolve(a.thread)
    deadline = time.time() + a.wait
    while True:
        job = job_state(sess.get("id"))
        state = activity(sess, job)
        if state == "blocked":
            raise C3Error(f"session is waiting on a person ({blocker(sess, job)['needs']}); "
                          f"answer it with `claude attach {sess.get('id')}` first")
        if state not in ("active", "waitingOnTasks"):
            break
        if time.time() > deadline:
            raise C3Error(f"the session is still working after {a.wait}s; use `steer`, `stop`, or retry")
        time.sleep(POLL)
        sess = refresh(sess)
    if state == "idle":
        # `claude --bg --resume` copies a session that is still running, so stop the idle process first.
        stop_session(sess)
    new, turn_id = resume_with(sess, message)
    job = job_state(new.get("id")) or {}
    return {"queued": True, "threadId": new["sessionId"], "turnId": turn_id,
            "launchFlags": job.get("respawnFlags")}


def descendants(pid):
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout
    except OSError:
        return []
    children = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, stack = [], [pid]
    while stack:
        for c in children.get(stack.pop(), []):
            found.append(c)
            stack.append(c)
    return found


def cmd_stop(a):
    sess = resolve(a.thread)
    state = activity(sess, job_state(sess.get("id")))
    turn = load(sess).current
    active = turn["id"] if turn and turn["status"] == "inProgress" and state != "notLoaded" else None
    procs = descendants(sess["pid"]) if a.kill_processes and "pid" in sess else []
    if "pid" in sess:
        stop_session(sess)
    out = {"interrupted": bool(active), "turnId": active, "stopped": "pid" in sess,
           "resume": f"claude --resume {sess['sessionId']}"}
    if a.kill_processes:
        killed = []
        for p in procs:
            try:
                os.kill(p, signal.SIGTERM)
                killed.append(p)
            except OSError:
                pass
        out["processesKilled"] = len(killed)
    else:
        out["note"] = "commands the session started may still be running; pass --kill-processes to end them"
    return out


def cmd_archive(a):
    sess = resolve(a.thread)
    if sess.get("kind") == "transcript":
        raise C3Error(f"{sess['sessionId']} is not a background session; nothing to archive")
    procs = descendants(sess["pid"]) if "pid" in sess else []
    run_claude(["rm", sess.get("id") or sess["sessionId"]])
    for p in procs:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    return {"archived": True, "threadId": sess["sessionId"],
            "note": f"removed from `claude agents`; the transcript is kept: `claude --resume {sess['sessionId']}`"}


def cmd_list(a):
    rows = sorted(agents(), key=lambda s: s.get("startedAt") or 0, reverse=True)
    if not a.all:
        rows = [s for s in rows if s.get("kind") == "background"]
    out = []
    for s in rows[:a.limit]:
        job = job_state(s.get("id")) or {}
        started = s.get("startedAt")
        out.append({"id": s.get("sessionId"), "shortId": s.get("id"), "name": s.get("name"),
                    "kind": s.get("kind"), "status": activity(s, job), "cwd": s.get("cwd"),
                    "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started / 1000))
                    if started else None,
                    "preview": clip(job.get("detail") or job.get("intent"), 200)})
    return {"threads": out}


def cmd_models(_a):
    return {"models": [{"id": m, "efforts": EFFORTS} for m in MODEL_ALIASES],
            "efforts": EFFORTS, "serviceTiers": {"priority": "Fast mode (fastMode setting)",
                                                 "default": "standard speed"},
            "note": "aliases resolve to the latest model of each family; full model ids "
                    "(e.g. claude-opus-5-5) are also accepted. Fast mode applies only to models that support it"}


# ---------------------------------------------------------------- CLI

def build_parser():
    p = argparse.ArgumentParser(prog="c3tl", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_, description=help_)
        s.set_defaults(fn=fn)
        return s

    def add_turn_flags(s, tier=None):
        s.add_argument("--model", help="model alias or id, see `models`")
        s.add_argument("--effort", choices=EFFORTS, help="effort level")
        s.add_argument("--tier", default=tier, choices=["priority", "default"],
                       help="`priority` (Fast mode) or `default`" + (f" (default: {tier})" if tier else ""))

    def add_session_flags(s, tier=None):
        add_turn_flags(s, tier)
        s.add_argument("--permission-mode", default=DEFAULT_PERMISSION_MODE, choices=PERMISSION_MODES,
                       help=f"default: {DEFAULT_PERMISSION_MODE}")
        s.add_argument("--cwd", default=os.getcwd(), help="working directory (default: current); "
                                                          "Claude Code must already trust it")
        s.add_argument("--name", help="session name shown in `claude agents` and /resume")
        s.add_argument("-c", "--config", action="append", metavar="KEY=VALUE",
                       help="Claude Code setting override, dotted keys allowed; VALUE is parsed as JSON, "
                            "else string")

    s = add("start", cmd_start, "start a new background session and its first turn; returns immediately "
                                "unless --wait")
    s.add_argument("prompt", help="prompt text, or - to read it from stdin")
    add_session_flags(s)
    s.add_argument("--wait", type=int, metavar="SECONDS",
                   help="block until the session settles (up to SECONDS) and print a compact result; "
                        "the session keeps running if c3tl exits first")

    s = add("review", cmd_review,
            "run Claude Code's /code-review in a new background session and wait for the result "
            "(default target: uncommitted changes)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--base", metavar="BRANCH", help="review the current branch against BRANCH")
    g.add_argument("--commit", metavar="SHA", help="review the changes introduced by one commit")
    g.add_argument("--instructions", help="custom review target or instructions")
    s.add_argument("--level", choices=REVIEW_LEVELS, help="review depth passed to /code-review")
    s.add_argument("--wait", type=int, default=3600, help="max seconds to wait for the review (default 3600)")
    add_session_flags(s, tier="default")

    s = add("status", cmd_status, "session state and the latest transcript items")
    s.add_argument("thread")
    s.add_argument("--last", type=int, default=8, help="number of recent items (default 8)")
    s.add_argument("--turns", type=int, default=2, help="number of recent turns to scan (default 2)")
    s.add_argument("--chars", type=int, default=1500, help="max chars per text field (keeps the tail)")

    s = add("watch", cmd_watch, "collect live events of the active turn for up to --seconds")
    s.add_argument("thread")
    s.add_argument("--seconds", type=float, default=60)
    s.add_argument("--max-events", type=int, default=40)
    s.add_argument("--chars", type=int, default=800)
    s.add_argument("--wait-start", action="store_true", help="keep listening even if no turn is active yet")

    s = add("wait", cmd_wait, "block until the session settles and print a compact result "
                              "(final message, failed commands, changed files); returns the last turn's "
                              "result at once if the session is idle")
    s.add_argument("thread")
    s.add_argument("--seconds", type=int, default=3600, help="max seconds to wait (default 3600)")

    s = add("result", cmd_result, "full text of the latest agent message (and /code-review output, if any)")
    s.add_argument("thread")

    s = add("steer", cmd_steer, "redirect the running turn: interrupt it and continue with this message")
    s.add_argument("thread")
    s.add_argument("message", help="message text, or - to read it from stdin")

    s = add("queue", cmd_queue, "wait for the session to settle, then start a new turn (blocks)")
    s.add_argument("thread")
    s.add_argument("message", help="message text, or - to read it from stdin")
    s.add_argument("--wait", type=int, default=3600, help="max seconds to wait (default 3600)")

    s = add("stop", cmd_stop, "stop the session (interrupting any active turn); it stays resumable")
    s.add_argument("thread")
    s.add_argument("--kill-processes", action="store_true",
                   help="also end commands the session started")

    s = add("archive", cmd_archive, "remove a background session from `claude agents` and end its processes; "
                                    "the transcript is kept")
    s.add_argument("thread")

    s = add("list", cmd_list, "recent background sessions")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--all", action="store_true", help="include interactive sessions")

    add("models", cmd_models, "model aliases, effort levels, and service tiers")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        out = a.fn(a)
    except (C3Error, OSError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
