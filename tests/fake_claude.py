#!/usr/bin/env python3
"""Stand-in for the `claude` CLI: background-session commands backed by files in CLAUDE_CONFIG_DIR.

fake.json holds {"agents": [...], "calls": [...], "next": [short ids to hand out], "reply": text,
"untrusted": [cwds], "copy_on_flags": bool}. Each call is appended to "calls".
"""
import json
import os
import sys
import uuid

home = os.environ["CLAUDE_CONFIG_DIR"]
state_file = os.path.join(home, "fake.json")
with open(state_file) as f:
    st = json.load(f)
args = sys.argv[1:]
st["calls"].append({"args": args, "cwd": os.getcwd()})


def save():
    with open(state_file, "w") as f:
        json.dump(st, f)


def transcript(sid, cwd):
    d = os.path.join(home, "projects", cwd.replace("/", "-"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{sid}.jsonl")


def write_turn(sid, cwd, prompt):
    pid = str(uuid.uuid4())
    recs = [{"type": "user", "promptId": pid, "cwd": cwd, "message": {"role": "user", "content": prompt}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": st.get("reply", "ok")}]}},
            {"type": "system", "subtype": "turn_duration", "durationMs": 5}]
    with open(transcript(sid, cwd), "a") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def find(ref):
    return next((a for a in st["agents"] if ref in (a.get("id"), a.get("sessionId"))), None)


if args[:1] == ["agents"]:
    save()
    print(json.dumps(st["agents"]))
elif args[:1] in (["stop"], ["rm"]):
    a = find(args[1])
    if a is None:
        save()
        print(f"no session {args[1]}", file=sys.stderr)
        sys.exit(1)
    if args[0] == "rm":
        st["agents"].remove(a)
    else:
        a.pop("pid", None)
        a["status"] = None
    save()
    print(f"{'stopped' if args[0] == 'stop' else 'removed'} {a['id']}")
elif args[:1] == ["--bg"]:
    cwd = os.getcwd()
    if cwd in st.get("untrusted", []):
        save()
        print(f"Workspace not trusted. Run `claude` in {cwd} once and accept the trust prompt, then retry.",
              file=sys.stderr)
        sys.exit(1)
    prompt = args[args.index("--") + 1]
    flags = args[1:args.index("--")]
    if "--resume" in flags:
        sid = flags[flags.index("--resume") + 1]
        a = find(sid) or {}
        if len(flags) > 2 or "pid" in a:
            short = st["next"].pop(0)
            new = {"id": short, "sessionId": short + "-0000-4000-8000-000000000000", "cwd": cwd,
                   "kind": "background", "pid": 1, "status": "idle", "state": "working", "startedAt": 2}
            st["agents"].append(new)
            save()
            print(f"note: started a copy as {short}.\nbackgrounded · {short}")
            sys.exit(0)
        a.update(pid=1, status="idle")
        write_turn(a["sessionId"], a["cwd"], prompt)
        save()
        print(f"backgrounded · {a['id']} · {a.get('name')}")
    else:
        short = st["next"].pop(0)
        sid = short + "-1111-4000-8000-000000000000"
        name = flags[flags.index("--name") + 1] if "--name" in flags else None
        st["agents"].append({"id": short, "sessionId": sid, "cwd": cwd, "kind": "background", "pid": 1,
                             "status": "idle", "state": "working", "name": name, "startedAt": 1})
        write_turn(sid, cwd, prompt)
        save()
        print(f"Starting background service…\nbackgrounded · {short} · {name}")
else:
    save()
    print(f"unexpected args {args}", file=sys.stderr)
    sys.exit(2)
