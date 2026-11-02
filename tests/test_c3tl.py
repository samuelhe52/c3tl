import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
os.environ["C3TL_POLL"] = "0.01"
import c3tl  # noqa: E402

FAKE = os.path.join(HERE, "fake_claude.py")


def rec(**kw):
    return json.dumps(kw) + "\n"


def user(text, pid, **kw):
    return rec(type="user", promptId=pid, message={"role": "user", "content": text}, **kw)


def assistant(*blocks, **kw):
    return rec(type="assistant", message={"role": "assistant", "content": list(blocks)}, **kw)


def tool_use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def tool_result(tid, content, is_error=False, pid="p1"):
    return rec(type="user", promptId=pid, message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": is_error}]})


DONE = rec(type="system", subtype="turn_duration", durationMs=7)


class Env(unittest.TestCase):
    """A temporary CLAUDE_CONFIG_DIR with the fake claude CLI on C3TL_CLAUDE."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.cwd = tempfile.mkdtemp()
        os.chmod(FAKE, 0o755)
        self.env = unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.home, "C3TL_CLAUDE": FAKE})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.state({"agents": [], "calls": [], "next": ["aaaa1111", "bbbb2222"], "reply": "ok"})

    def state(self, st=None):
        path = os.path.join(self.home, "fake.json")
        if st is not None:
            with open(path, "w") as f:
                json.dump(st, f)
        with open(path) as f:
            return json.load(f)

    def add_session(self, short, lines, job=None, **agent):
        sid = short + "-2222-4000-8000-000000000000"
        st = self.state()
        st["agents"].append({"id": short, "sessionId": sid, "cwd": self.cwd, "kind": "background",
                             "startedAt": 1, **agent})
        self.state(st)
        d = os.path.join(self.home, "projects", "proj")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, sid + ".jsonl"), "w") as f:
            f.write("".join(lines))
        if job is not None:
            os.makedirs(os.path.join(self.home, "jobs", short), exist_ok=True)
            with open(os.path.join(self.home, "jobs", short, "state.json"), "w") as f:
                json.dump(job, f)
        return sid

    def run_cli(self, *argv, stdin=None):
        buf = io.StringIO()
        code = 0
        with redirect_stdout(buf), unittest.mock.patch("sys.stdin", io.StringIO(stdin or "")):
            try:
                c3tl.main(list(argv))
            except SystemExit as e:
                code = e.code
        return json.loads(buf.getvalue()), code

    def bg_calls(self):
        return [c for c in self.state()["calls"] if c["args"][:1] == ["--bg"]]


class TranscriptTest(unittest.TestCase):
    def parse(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        f.write(text)
        f.close()
        tr = c3tl.Transcript(f.name)
        tr.read()
        return tr

    def test_turns_items_and_results(self):
        tr = self.parse(
            user("fix it", "p1")
            + assistant({"type": "thinking", "thinking": ""}, {"type": "text", "text": "Looking."},
                        tool_use("t1", "Bash", command="make test"), tool_use("t2", "Edit", file_path="/r/a.py"),
                        tool_use("t3", "mcp__github__get_pr"))
            + tool_result("t1", "Exit code 2\nboom", is_error=True)
            + tool_result("t2", "ok")
            + tool_result("t3", "denied", is_error=True)
            + rec(type="user", isSidechain=True, promptId="zz", message={"role": "user", "content": "side"})
            + assistant({"type": "text", "text": "Done."}) + DONE
            + user("<task-notification>\n<task-id>x</task-id>", "p2")
            + assistant({"type": "text", "text": "Saw it."}))
        self.assertEqual([t["id"] for t in tr.turns], ["p1", "p2"])
        first, second = tr.turns
        self.assertEqual((first["status"], first["durationMs"], second["origin"]), ("completed", 7, "taskNotification"))
        cmd, edit, mcp = [i for i in first["items"] if i["type"] not in ("userMessage", "agentMessage")]
        self.assertEqual((cmd["status"], cmd["exitCode"], cmd["aggregatedOutput"]), ("failed", 2, "Exit code 2\nboom"))
        self.assertEqual((edit["type"], edit["paths"], edit["status"]), ("fileChange", ["/r/a.py"], "completed"))
        self.assertEqual((mcp["server"], mcp["tool"], mcp["error"]), ("github", "get_pr", "denied"))
        s = c3tl.turn_summary(tr.turns[:1])
        self.assertEqual((s["text"], s["failedCount"], s["filesChanged"]), ("Done.", 1, ["/r/a.py"]))
        self.assertEqual(second["status"], "inProgress")
        tr.settle("active")
        self.assertEqual(second["status"], "inProgress")
        tr.settle("notLoaded")
        self.assertEqual(second["status"], "interrupted")

    def test_slash_command_turn_closes_on_local_command(self):
        tr = self.parse(user("<local-command-caveat>x</local-command-caveat>", "p1", isMeta=True)
                        + user("/code-review low", "p1") + rec(type="system", subtype="local_command"))
        self.assertEqual(len(tr.turns), 1)
        self.assertEqual((tr.turns[0]["origin"], tr.turns[0]["status"]), ("command", "completed"))
        self.assertEqual([i["text"] for i in tr.turns[0]["items"]], ["/code-review low"])

    def test_partial_last_line_waits_for_next_read(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        whole = user("hi", "p1")
        f.write(whole + DONE[:10])
        f.close()
        tr = c3tl.Transcript(f.name)
        tr.read()
        self.assertEqual(tr.turns[0]["status"], "inProgress")
        with open(f.name, "a") as g:
            g.write(DONE[10:])
        self.assertEqual([e["event"] for e in tr.read()], ["turnCompleted"])


class StartTest(Env):
    def test_start_passes_flags_and_returns_ids(self):
        out, code = self.run_cli("start", "-", "--cwd", self.cwd, "--model", "opus", "--effort", "high",
                                 "--tier", "priority", "-c", "env.FOO=1", "--name", "job", stdin="-x prompt")
        self.assertEqual(code, 0, out)
        call = self.bg_calls()[0]
        self.assertEqual(call["cwd"], os.path.realpath(self.cwd))
        args = call["args"]
        self.assertEqual(args[-2:], ["--", "-x prompt"])
        self.assertEqual(args[args.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(json.loads(args[args.index("--settings") + 1]), {"env": {"FOO": 1}, "fastMode": True})
        self.assertEqual((args[args.index("--model") + 1], args[args.index("--name") + 1]), ("opus", "job"))
        self.assertEqual(out["threadId"], "aaaa1111-1111-4000-8000-000000000000")
        self.assertEqual(out["attach"], "claude attach aaaa1111")
        self.assertIsNotNone(out["turnId"])

    def test_start_wait_returns_compact_result(self):
        out, _ = self.run_cli("start", "go", "--cwd", self.cwd, "--wait", "5")
        self.assertEqual((out["finished"], out["turnStatus"], out["text"]), (True, "completed", "ok"))

    def test_blank_prompt_is_rejected_before_launch(self):
        out, code = self.run_cli("start", "-", "--cwd", self.cwd, stdin="  \n")
        self.assertEqual((code, out), (1, {"error": "empty message on stdin"}))
        self.assertEqual(self.bg_calls(), [])

    def test_untrusted_workspace_error_is_reported(self):
        st = self.state()
        st["untrusted"] = [os.path.realpath(self.cwd)]
        self.state(st)
        out, code = self.run_cli("start", "go", "--cwd", self.cwd)
        self.assertEqual(code, 1)
        self.assertIn("Workspace not trusted", out["error"])


class ContinueTest(Env):
    def test_queue_stops_idle_session_then_resumes_without_flags(self):
        sid = self.add_session("cccc3333", [user("one", "p1"), assistant({"type": "text", "text": "1"}), DONE],
                               job={"inFlight": {"tasks": 0, "queued": 1}}, pid=5, status="idle")
        out, code = self.run_cli("queue", "cccc3333", "two")
        self.assertEqual(code, 0, out)
        calls = [c["args"] for c in self.state()["calls"] if c["args"][0] != "agents"]
        self.assertEqual(calls, [["stop", "cccc3333"], ["--bg", "--resume", sid, "--", "two"]])
        self.assertTrue(out["queued"])
        self.assertNotEqual(out["turnId"], "p1")

    def test_queue_refuses_a_copy(self):
        self.add_session("cccc3333", [user("one", "p1"), DONE], pid=5, status="idle")
        with unittest.mock.patch.object(c3tl, "stop_session"):  # leave it running, so claude makes a copy
            out, code = self.run_cli("queue", "cccc3333", "two")
        self.assertEqual(code, 1)
        self.assertIn("started a copy", out["error"])

    def test_queue_refuses_blocked_session(self):
        self.add_session("cccc3333", [user("one", "p1")], job={"needs": "approve Write: /a"},
                         pid=5, status="waiting", state="blocked")
        out, code = self.run_cli("queue", "cccc3333", "two")
        self.assertEqual(code, 1)
        self.assertIn("approve Write: /a", out["error"])

    def test_steer_requires_active_turn(self):
        self.add_session("cccc3333", [user("one", "p1"), DONE], pid=5, status="idle")
        out, code = self.run_cli("steer", "cccc3333", "focus")
        self.assertEqual(code, 1)
        self.assertIn("no active turn", out["error"])

    def test_steer_interrupts_and_continues(self):
        self.add_session("cccc3333", [user("one", "p1")], pid=5, status="busy")
        out, code = self.run_cli("steer", "cccc", "focus")
        self.assertEqual(code, 0, out)
        self.assertEqual((out["steered"], out["interruptedTurnId"]), (True, "p1"))
        self.assertEqual([c["args"][0] for c in self.state()["calls"] if c["args"][0] != "agents"], ["stop", "--bg"])


class WaitTest(Env):
    def test_wait_holds_while_background_tasks_run(self):
        self.add_session("cccc3333", [user("one", "p1"), DONE], job={"inFlight": {"tasks": 1}}, pid=5, status="idle")
        out, _ = self.run_cli("wait", "cccc3333", "--seconds", "0")
        self.assertEqual((out["finished"], out["threadStatus"]), (False, "waitingOnTasks"))

    def test_wait_reports_blocked_session(self):
        self.add_session("cccc3333", [user("one", "p1")], job={"needs": "approve Bash: rm x"},
                         pid=5, status="waiting", state="blocked")
        out, _ = self.run_cli("wait", "cccc3333", "--seconds", "5")
        self.assertFalse(out["finished"])
        self.assertEqual(out["errors"][-1]["needs"], "approve Bash: rm x")

    def test_watch_on_blocked_session_is_not_finished(self):
        self.add_session("cccc3333", [user("one", "p1")], job={"needs": "approve Write: /a"},
                         pid=5, status="waiting", state="blocked")
        out, _ = self.run_cli("watch", "cccc3333", "--seconds", "1")
        self.assertEqual((out["finished"], out["threadStatus"]), (False, "blocked"))
        self.assertEqual(out["events"][-1]["needs"], "approve Write: /a")

    def test_wait_on_stopped_session_reports_interrupted_turn(self):
        self.add_session("cccc3333", [user("one", "p1"), DONE, user("two", "p2")])
        out, _ = self.run_cli("wait", "cccc3333")
        self.assertEqual((out["finished"], out["turnId"], out["turnStatus"]), (True, "p2", "interrupted"))


class ResolveAndReviewTest(Env):
    def test_resolve_by_prefix_name_and_ambiguity(self):
        self.add_session("dddd4444", [], name="alpha")
        self.add_session("dddd5555", [], name="beta")
        self.assertEqual(c3tl.resolve("beta")["id"], "dddd5555")
        self.assertEqual(c3tl.resolve("dddd44")["id"], "dddd4444")
        with self.assertRaisesRegex(c3tl.C3Error, "matches 2 sessions"):
            c3tl.resolve("dddd")

    def test_review_targets_commit_and_returns_reviewer_output(self):
        def fake_review(sid, since=0.0):
            return {"review": "(none)", "findings": None, "reviewFinished": True}
        with unittest.mock.patch.object(c3tl, "review_output", fake_review):
            out, code = self.run_cli("review", "--cwd", self.cwd, "--commit", "abc123", "--level", "low")
        self.assertEqual(code, 0, out)
        args = self.bg_calls()[0]["args"]
        self.assertEqual(args[-1], "/code-review low abc123^!")
        self.assertEqual(json.loads(args[args.index("--settings") + 1]), {"fastMode": False})
        self.assertEqual((out["review"], out["finished"], out["threadStatus"]), ("(none)", True, "notLoaded"))
        self.assertIn(["stop", "aaaa1111"], [c["args"] for c in self.state()["calls"]])

    def test_review_output_reads_finished_skill_agent(self):
        sid = self.add_session("eeee6666", [])
        sub = os.path.join(self.home, "projects", "proj", sid, "subagents")
        os.makedirs(sub)
        with open(os.path.join(sub, "agent-x.forked-skill.json"), "w") as f:
            json.dump({"skillName": "code-review"}, f)
        with open(os.path.join(sub, "agent-x.meta.json"), "w") as f:
            json.dump({"finished": True}, f)
        with open(os.path.join(sub, "agent-x.jsonl"), "w") as f:
            f.write(assistant({"type": "text", "text": "draft"}, tool_use("r", "ReportFindings", findings=[{"file": "a"}]))
                    + assistant({"type": "text", "text": "final"}))
        self.assertEqual(c3tl.review_output(sid),
                         {"review": "final", "findings": [{"file": "a"}], "reviewFinished": True})


if __name__ == "__main__":
    unittest.main()
