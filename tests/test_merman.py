import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import merman  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="merman-tests-")
FAKE_JERRY = os.path.join(_TMP, "jerry")  # copied so it is executable even if the checkout lost the mode bit
with open(os.path.join(HERE, "fake_jerry.py")) as _src, open(FAKE_JERRY, "w") as _dst:
    _dst.write(_src.read())
os.chmod(FAKE_JERRY, 0o755)


def parse(text):
    return merman.parse_diagram(text)


def codes(text, known=()):
    return sorted(g.code for g in merman.lint(parse(text), set(known)))


def cli(diagram, *args, env=None, stdin=None, suffix=".mmd"):
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as f:
        f.write(diagram)
        path = f.name
    try:
        full_env = dict(os.environ)
        full_env.update(env or {})
        cmd = [sys.executable, os.path.join(ROOT, "merman.py")]
        args = list(args)
        cmd += [args[0], path] + args[1:]
        p = subprocess.run(cmd, capture_output=True, text=True, input=stdin, env=full_env, timeout=60)
        return p.returncode, p.stdout, p.stderr
    finally:
        os.unlink(path)


class ParserTests(unittest.TestCase):
    def test_shapes_and_kinds(self):
        d = parse('''flowchart TD
            a["1 + 1"]
            b[["echo hi"]]
            c{{"Is it ${x}?"}}
            e{"x > 1"}
            f("sh: ls")
            g["py: 2 * 3"]
            h["ask: hello"]
            i["total := 4"]
        ''')
        kinds = {k: n.kind for k, n in d.nodes.items()}
        self.assertEqual(kinds, {"a": "python", "b": "shell", "c": "llm", "e": "python",
                                 "f": "shell", "g": "python", "h": "llm", "i": "python"})
        self.assertEqual(d.nodes["f"].body, "ls")
        self.assertEqual(d.nodes["i"].bind, "total")
        self.assertEqual(d.nodes["i"].body, "4")

    def test_edge_forms(self):
        d = parse('''graph LR
            A --> B
            A -->|x| C
            A -- y --> D
            A -- "z w" --> E
            A ==> F
            A -.-> G
            A -. dotted .-> H
            A -->|"[1..5)"| I
        ''')
        labels = [(e.dst, e.label) for e in d.edges]
        self.assertEqual(labels, [("B", None), ("C", "x"), ("D", "y"), ("E", "z w"), ("F", None),
                                  ("G", None), ("H", "dotted"), ("I", "[1..5)")])

    def test_chains_and_fan(self):
        d = parse("flowchart TD\n A --> B --> C\n D & E --> F & G\n")
        pairs = [(e.src, e.dst) for e in d.edges]
        self.assertEqual(pairs, [("A", "B"), ("B", "C"), ("D", "F"), ("D", "G"), ("E", "F"), ("E", "G")])

    def test_semicolon_inside_quotes_and_between_statements(self):
        d = parse('flowchart TD\n a["x; y"] --> b; b --> c\n')
        self.assertEqual(d.nodes["a"].text, "x; y")
        self.assertEqual(len(d.edges), 2)

    def test_directives_comments_and_ignored_statements(self):
        d = parse('''flowchart TD
            %% a normal comment
            %% merman: start=b
            %% merman: timeout=5
            %% merman: a timeout=9
            %% merman: allow-no-exit
            style a fill:#f9f
            classDef big font-size:20px
            a:::big --> b
            subgraph s
              b --> a
            end
        ''')
        self.assertEqual(d.start, "b")
        self.assertEqual(d.timeout, 5)
        self.assertEqual(d.node_timeouts, {"a": 9})
        self.assertTrue(d.allow_no_exit)
        self.assertEqual(len(d.subgraph_lines), 1)
        self.assertEqual(len(d.edges), 2)

    def test_entities_and_br(self):
        d = parse('flowchart TD\n a["x #gt; 1<br/>y #35; z"]\n')
        self.assertEqual(d.nodes["a"].text, "x > 1\ny # z")

    def test_entry_is_first_mentioned(self):
        self.assertEqual(parse("flowchart TD\n b --> a\n a --> c\n").entry, "b")

    def test_errors(self):
        for bad in ["a --> b", "flowchart TD\n a --- b", "flowchart TD\n a[\"x\n", "flowchart TD\n a --> \n"]:
            with self.assertRaises(merman.ParseError, msg=bad):
                parse(bad)

    def test_markdown_blocks(self):
        text = "x\n```mermaid\nflowchart TD\n a --> b\n```\n\n```mermaid\nflowchart TD\n c --> d\n```\n"
        blocks = merman.extract_blocks(text)
        self.assertEqual([b[1] for b in blocks], [3, 8])


class LabelTests(unittest.TestCase):
    def m(self, s):
        return merman.parse_label(s)

    def test_equality_and_literal(self):
        self.assertTrue(self.m("ok").matches("ok"))
        self.assertFalse(self.m("ok").matches("OK"))
        self.assertTrue(self.m("'*'").matches("*"))
        self.assertFalse(self.m("'*'").matches("x"))
        self.assertTrue(self.m("'else'").matches("else"))
        self.assertTrue(self.m("'<=5'").matches("<=5"))
        self.assertIsInstance(self.m("else"), merman.Default)
        self.assertIsInstance(self.m("*"), merman.Default)

    def test_regex(self):
        r = self.m("/^5[0-9][0-9]$/")
        self.assertTrue(r.matches("503"))
        self.assertFalse(r.matches("403"))
        self.assertTrue(self.m("/abc/i").matches("xxABCxx"))
        with self.assertRaises(merman.LabelError):
            self.m("/(/")

    def test_ranges(self):
        r = self.m("[1..5)")
        self.assertTrue(r.matches("1") and r.matches("4.9"))
        self.assertFalse(r.matches("5") or r.matches("0") or r.matches("abc"))
        self.assertTrue(self.m("(..5]").matches("-100"))
        self.assertFalse(self.m("(..5]").matches("5.1"))
        self.assertTrue(self.m("[10..)").matches("10"))
        self.assertFalse(self.m("(10..)").matches("10"))
        lex = self.m("[a..f]")
        self.assertTrue(lex.matches("c") and lex.matches("f"))
        self.assertFalse(lex.matches("g"))
        self.assertTrue(self.m("[1.5..2.5]").matches("2"))
        with self.assertRaises(merman.LabelError):
            self.m("[5..1]")

    def test_comparisons(self):
        self.assertTrue(self.m("<5").matches("4") and not self.m("<5").matches("5"))
        self.assertTrue(self.m("<=5").matches("5"))
        self.assertTrue(self.m(">= 80").matches("80.0"))
        self.assertFalse(self.m(">80").matches("80"))
        self.assertTrue(self.m(">80").matches("81"))
        self.assertFalse(self.m(">80").matches("abc"))
        self.assertTrue(self.m("!=1").matches("2"))
        self.assertFalse(self.m("!=1").matches("1.0"))
        self.assertTrue(self.m("!=1").matches("abc"))
        self.assertTrue(self.m(">m").matches("z"))  # lexical
        with self.assertRaises(merman.LabelError):
            self.m(">=")

    def test_glob(self):
        g = self.m("*.txt")
        self.assertTrue(g.matches("a.txt"))
        self.assertFalse(g.matches("a.txt.bak"))
        self.assertTrue(self.m("a?c").matches("abc"))
        self.assertFalse(self.m("a?c").matches("ac"))
        self.assertTrue(self.m("[x]*").matches("[x]yz"))  # brackets are not special

    def test_select_edge_order_and_default(self):
        d = parse('flowchart TD\n a -->|"/^b/"| x\n a -->|bar| y\n a -->|else| z\n')
        es = d.outgoing()["a"]
        self.assertEqual(merman.select_edge(es, "bar").dst, "x")  # first match wins
        self.assertEqual(merman.select_edge(es, "q").dst, "z")

    def test_system_message(self):
        d = parse('flowchart TD\n a -->|bug| x\n a -->|billing| y\n a -->|"[1..5)"| z\n a -->|else| w\n')
        msg = merman.build_system_message(d.outgoing()["a"])
        self.assertIn("- bug\n- billing", msg)
        self.assertIn("a number from 1 up to but not including 5", msg)
        self.assertIn("short answer of your own", msg)


class LintTests(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(codes('flowchart TD\n a["1"] -->|1| b["2"]\n a -->|else| c["3"]\n'), [])

    def test_errors(self):
        self.assertIn("E002", codes('flowchart TD\n a --> b\n a -->|x| c\n'))
        self.assertIn("E003", codes('flowchart TD\n a --> b\n a --> c\n'))
        self.assertIn("E004", codes('flowchart TD\n a -->|else| b\n a -->|*| c\n'))
        self.assertIn("E005", codes('flowchart TD\n a -->|"[5..1]"| b\n a -->|else| c\n'))
        self.assertIn("E006", codes('flowchart TD\n a[""]\n'))
        self.assertIn("E007", codes('flowchart TD\n %% merman: start=zz\n a --> b\n'))
        self.assertIn("E008", codes('flowchart TD\n %% merman: timeout=abc\n a --> b\n'))
        self.assertIn("E009", codes('flowchart TD\n a["x = 3"]\n'))

    def test_warnings(self):
        self.assertIn("W001", codes('flowchart TD\n a --> b\n b --> a\n'))
        self.assertNotIn("W001", codes('flowchart TD\n %% merman: allow-no-exit\n a --> b\n b --> a\n'))
        self.assertIn("W002", codes('flowchart TD\n a\n b --> c\n'))
        self.assertIn("W003", codes('flowchart TD\n a -->|x| b\n a -->|x| c\n a -->|else| d\n'))
        self.assertIn("W004", codes('flowchart TD\n a -->|"*.txt"| b\n a -->|"a.txt"| c\n a -->|else| d\n'))
        self.assertIn("W005", codes('flowchart TD\n a -->|"<10"| b\n a -->|"<=5"| c\n a -->|else| d\n'))
        self.assertNotIn("W005", codes('flowchart TD\n a -->|"<10"| b\n a -->|">=10"| c\n a -->|else| d\n'))
        self.assertIn("W006", codes('flowchart TD\n a -->|x| b\n a -->|y| c\n'))
        self.assertIn("W007", codes('flowchart TD\n a{{"hi ${who}"}}\n'))
        self.assertNotIn("W007", codes('flowchart TD\n a{{"hi ${who}"}}\n', known=["who"]))
        self.assertIn("W008", codes('flowchart TD\n subgraph s\n a --> b\n end\n'))
        self.assertIn("W009", codes('flowchart TD\n %% merman: bogus=1\n a --> b\n'))
        self.assertIn("W010", codes('flowchart TD\n a --> b\n'))

    def test_lint_cli(self):
        rc, out, _ = cli('flowchart TD\n a["1"] --> b["2"]\n', "lint")
        self.assertEqual((rc, out.strip()), (0, "ok"))
        rc, out, _ = cli('flowchart TD\n a["1"] -->|x| b["2"]\n', "lint")
        self.assertEqual(rc, 0)
        self.assertIn("W006", out)
        rc, _, _ = cli('flowchart TD\n a["1"] -->|x| b["2"]\n', "lint", "--strict")
        self.assertEqual(rc, 65)
        rc, out, _ = cli('flowchart TD\n a["1"] -->|x| b["2"]\n', "lint", "--ignore", "W006", "--strict")
        self.assertEqual(rc, 0)
        rc, _, err = cli('flowchart TD\n a["1"]\n', "lint", "--ignore", "E001")
        self.assertEqual(rc, 64)


class RunTests(unittest.TestCase):
    def test_python_terminal_and_binding(self):
        rc, out, _ = cli('flowchart TD\n a["n := 20 + 1"] --> b["n * 2"]\n', "run")
        self.assertEqual((rc, out), (0, "42\n"))

    def test_shell_branching_and_exit_code_forwarding(self):
        d = '''flowchart TD
            c[["test -f /nonexistent"]] -->|0| ok["'yes'"]
            c -->|else| bad[["echo no; exit 3"]]
        '''
        rc, out, _ = cli(d, "run")
        self.assertEqual((rc, out), (3, "no\n"))

    def test_retry_loop(self):
        d = '''flowchart TD
            init["n := 0"] --> check
            check[["exit 1"]] -->|0| ok["'healthy'"]
            check -->|else| bump["n := n + 1"]
            bump -->|"<=2"| check
            bump -->|else| fail[["echo gave up >&2; exit 9"]]
        '''
        rc, out, err = cli(d, "run")
        self.assertEqual(rc, 9)
        self.assertIn("gave up", err)

    def test_set_vars_env_and_context(self):
        d = '''flowchart TD
            a[["echo $name"]] --> b["_out + '!' + str(_rc)"]
        '''
        rc, out, _ = cli(d, "run", "--set", "name=bob")
        self.assertEqual((rc, out), (0, "bob!0\n"))

    def test_shell_binding_is_stripped_stdout(self):
        d = 'flowchart TD\n a["v := sh: printf \'  hi  \\n\'"] --> b["v.upper()"]\n'
        rc, out, err = cli(d, "run")
        self.assertEqual((rc, out), (0, "HI\n"), err)

    def test_stdin_json(self):
        rc, out, _ = cli('flowchart TD\n a["x[\'k\'] + n"]\n', "run", "--stdin-json", stdin='{"x": {"k": 2}, "n": 3}')
        self.assertEqual((rc, out), (0, "5\n"))
        rc, _, _ = cli('flowchart TD\n a["1"]\n', "run", "--stdin-json", stdin="[1]")
        self.assertEqual(rc, 64)
        rc, out, _ = cli('flowchart TD\n a["n"]\n', "run", "--stdin-json", "--set", "n=2", stdin='{"n": 1}')
        self.assertEqual(out, "2\n")

    def test_no_match_is_runtime_error(self):
        d = 'flowchart TD\n a["\'zzz\'"] -->|x| b["1"]\n'
        rc, _, err = cli(d, "run", "--ignore", "W006")
        self.assertEqual(rc, 70)
        self.assertIn("R001", err)

    def test_step_limit(self):
        rc, _, err = cli('flowchart TD\n a["1"] --> b["2"]\n b --> a\n', "run", "--max-steps", "5", "--ignore", "W001")
        self.assertEqual(rc, 70)
        self.assertIn("R003", err)

    def test_timeouts(self):
        rc, _, err = cli('flowchart TD\n a[["sleep 5"]] --> b["1"]\n', "run", "--timeout", "0.3")
        self.assertEqual(rc, 70)
        self.assertIn("R004", err)
        rc, _, err = cli('flowchart TD\n %% merman: a timeout=0.3\n a["__import__(\'time\').sleep(5)"] --> b["1"]\n', "run")
        self.assertEqual(rc, 70)
        self.assertIn("R004", err)

    def test_python_exception(self):
        rc, _, err = cli('flowchart TD\n a["1/0"] --> b["2"]\n', "run")
        self.assertEqual(rc, 70)
        self.assertIn("ZeroDivisionError", err)

    def test_parse_error_exit(self):
        rc, _, err = cli("flowchart TD\n a --- b\n", "run")
        self.assertEqual(rc, 65)
        self.assertIn("E001", err)

    def test_dry_run_executes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "touched")
            rc, out, _ = cli(f'flowchart TD\n a[["touch {marker}"]] --> b["1"]\n', "run", "--dry-run")
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(marker))
            self.assertIn("a\tshell\ttouch", out)

    def test_echo_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            rep = os.path.join(tmp, "r.json")
            rc, out, _ = cli('flowchart TD\n a["x := 1"] --> b["x + 1"]\n', "run", "--echo", "--report", rep)
            self.assertEqual(out, "1\n2\n")
            with open(rep) as fh:
                data = json.load(fh)
            self.assertEqual(data["outcome"], "ok")
            self.assertEqual([p["node"] for p in data["path"]], ["a", "b"])
            self.assertEqual(data["context"]["x"], 1)

    def test_markdown_input(self):
        rc, out, _ = cli("# t\n```mermaid\nflowchart TD\n a[\"'hi'\"]\n```\n", "run", suffix=".md")
        self.assertEqual((rc, out), (0, "hi\n"))
        two = "```mermaid\nflowchart TD\n a[\"1\"]\n```\n```mermaid\nflowchart TD\n a[\"2\"]\n```\n"
        self.assertEqual(cli(two, "run", suffix=".md")[0], 65)
        self.assertEqual(cli(two, "run", "--block", "2", suffix=".md")[1], "2\n")


class LlmTests(unittest.TestCase):
    TRIAGE = '''flowchart TD
        classify{{"What kind of ticket is this? ${ticket}"}} -->|bug| eng["'engineering'"]
        classify -->|billing| fin["'finance'"]
        classify -->|else| human["'human'"]
    '''

    def run_llm(self, diagram, answers, *extra, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "log.jsonl")
            env = {"FAKE_JERRY_ANSWERS": answers, "FAKE_JERRY_LOG": log}
            env.update(kw.get("env", {}))
            rc, out, err = cli(diagram, "run", "--jerry", FAKE_JERRY, *extra, env=env)
            calls = []
            if os.path.exists(log):
                with open(log) as fh:
                    calls = [json.loads(l) for l in fh]
            return rc, out, err, calls

    def test_classification_and_prompt_assembly(self):
        rc, out, err, calls = self.run_llm(self.TRIAGE, "billing", "--set", "ticket=charged twice")
        self.assertEqual((rc, out), (0, "finance\n"), err)
        call = calls[0]
        # exactly one user message: the prompt followed by the answer format
        self.assertEqual(call["role"], "user")
        self.assertTrue(call["text"].startswith("What kind of ticket is this? charged twice\n\n"))
        self.assertIn("- bug\n- billing", call["text"])
        self.assertIn("short answer of your own", call["text"])
        self.assertNotIn("-S", call["args"])
        self.assertEqual(call["args"][:2], ["-s", "0"])
        self.assertNotIn("-f", call["args"])
        self.assertNotIn("-r", call["args"])

    def test_default_edge_catches_free_form_answer(self):
        rc, out, _, _ = self.run_llm(self.TRIAGE, "something else", "--set", "ticket=x")
        self.assertEqual(out, "human\n")

    def test_retries_use_new_seeds_only_without_default(self):
        d = '''flowchart TD
            q{{"pick"}} -->|a| x["'A'"]
            q -->|b| y["'B'"]
        '''
        rc, out, _, calls = self.run_llm(d, "junk,junk,b", "--llm-retries", "3", "--ignore", "W006")
        self.assertEqual((rc, out), (0, "B\n"))
        self.assertEqual([c["args"][c["args"].index("-s") + 1] for c in calls], ["0", "1", "2"])
        rc, _, err, calls = self.run_llm(d, "junk", "--llm-retries", "2", "--ignore", "W006")
        self.assertEqual(rc, 70)
        self.assertIn("R001", err)
        self.assertEqual(len(calls), 3)
        rc, out, _, calls = self.run_llm(self.TRIAGE, "junk", "--llm-retries", "5", "--set", "ticket=t")
        self.assertEqual(len(calls), 1)  # a default edge means no retries

    def test_terminal_llm_prints_response_without_system_message(self):
        rc, out, _, calls = self.run_llm('flowchart TD\n a{{"tell me a joke"}}\n', "ha")
        self.assertEqual((rc, out), (0, "ha\n"))
        self.assertEqual(calls[0]["text"], "tell me a joke")  # nothing appended without labeled edges

    def test_binding_and_templating(self):
        d = 'flowchart TD\n a["name := \'Ada\'"] --> b{{"ans := Hi ${name}, {braces} \\${raw}"}} --> c["ans"]\n'
        rc, out, _, calls = self.run_llm(d, "yo")
        self.assertEqual(out, "yo\n")
        self.assertEqual(calls[0]["text"], "Hi Ada, {braces} ${raw}")

    def test_jerry_failures(self):
        rc, _, err, _ = self.run_llm('flowchart TD\n a{{"x"}}\n', "ok", env={"FAKE_JERRY_EXIT": "2"})
        self.assertEqual(rc, 70)
        self.assertIn("jerry exited with code 2", err)
        rc, _, err = cli('flowchart TD\n a{{"x"}}\n', "run", "--jerry", "/nonexistent/jerry")
        self.assertEqual(rc, 70)
        self.assertIn("not found", err)

    def test_trace_shows_reasoning_prompts_and_raw_response(self):
        rc, out, err, calls = self.run_llm(self.TRIAGE, "Billing.", "--trace", "--set", "ticket=charged twice")
        self.assertEqual((rc, out), (0, "human\n"))  # "Billing." does not equal "billing"
        args = calls[0]["args"]
        self.assertIn("-r", args)
        self.assertNotIn("-f", args)
        for needle in ("asking jerry", "prompt>", "format>", "- bug", "charged twice",
                       "fake reasoning", "response> 'Billing.'"):
            self.assertIn(needle, err)
        rc, _, err, calls = self.run_llm(self.TRIAGE, "billing", "--set", "ticket=t")
        self.assertNotIn("-r", calls[0]["args"])
        self.assertNotIn("asking jerry", err)

    def test_jerry_stderr_is_passed_through_on_success(self):
        rc, out, err, _ = self.run_llm('flowchart TD\n a{{"x"}}\n', "ok", "--llm-arg=-r")
        self.assertEqual((rc, out), (0, "ok\n"))
        self.assertEqual(err.strip(), "fake reasoning")
        rc, out, err, _ = self.run_llm('flowchart TD\n a{{"x"}}\n', "ok")
        self.assertEqual(err, "")
        rc, out, err, _ = self.run_llm('flowchart TD\n a{{"x"}}\n', "ok", "--trace")
        self.assertEqual(err.count("fake reasoning"), 1)  # shown once, in the trace block

    def test_llm_arg_and_timeout(self):
        rc, _, _, calls = self.run_llm('flowchart TD\n a{{"x"}}\n', "ok", "--llm-arg", "-m some-model", "--timeout", "7")
        args = calls[0]["args"]
        self.assertNotIn("-f", args)
        self.assertEqual(args[-2:], ["-m", "some-model"])
        self.assertIn("7", args)


class TemplateTests(unittest.TestCase):
    def test_render_template(self):
        r = merman.render_template
        self.assertEqual(r("s=${score}", {"score": 7}), "s=7")
        self.assertEqual(r("s=${score:-85}", {}), "s=85")
        self.assertEqual(r("s=${score:-85}", {"score": 7}), "s=7")
        self.assertEqual(r("s=${score:-85}", {"score": ""}), "s=85")   # :- covers unset or empty
        self.assertEqual(r("s=${score-85}", {"score": ""}), "s=")      # - covers unset only
        self.assertEqual(r("s=${score-85}", {}), "s=85")
        self.assertEqual(r("x=${x:-}", {}), "x=")
        self.assertEqual(r("${a:-b c=d}", {}), "b c=d")
        self.assertEqual(r("\\${x} and ${x}", {"x": 1}), "${x} and 1")
        self.assertEqual(r('json {"a": {"b": 1}} {x} $x $5', {"x": 9}), 'json {"a": {"b": 1}} {x} $x $5')
        with self.assertRaises(KeyError):
            r("${score}", {})

    def test_lint_does_not_flag_defaulted_variables(self):
        self.assertNotIn("W007", codes('flowchart TD\n a{{"hi ${who:-world}"}}\n'))
        self.assertNotIn("W007", codes('flowchart TD\n a{{"hi ${who-world}"}}\n'))
        self.assertIn("W007", codes('flowchart TD\n a{{"hi ${who}"}}\n'))
        self.assertNotIn("W007", codes('flowchart TD\n a{{"hi \\${who}"}}\n'))

    def test_prompt_uses_default_and_set_overrides(self):
        d = 'flowchart TD\n a{{"score is ${score:-85}"}}\n'
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "log")
            env = {"FAKE_JERRY_LOG": log}
            cli(d, "run", "--jerry", FAKE_JERRY, env=env)
            cli(d, "run", "--jerry", FAKE_JERRY, "--set", "score=12", env=env)
            cli(d, "run", "--jerry", FAKE_JERRY, "--set", "score=", env=env)
            with open(log) as fh:
                prompts = [json.loads(l)["text"] for l in fh]
        self.assertEqual(prompts, ["score is 85", "score is 12", "score is 85"])


class AllBlocksTests(unittest.TestCase):
    DOC = """intro
```mermaid
flowchart TD
    a["x := 40"] --> b["'one'"]
```
text
```mermaid
flowchart TD
    c["x + 2"]
```
```python
print("not a diagram")
```
```mermaid
flowchart TD
    d[["echo three"]]
```
"""

    def test_runs_all_in_order_with_shared_context(self):
        rc, out, err = cli(self.DOC, "run", "--all-blocks", suffix=".md")
        self.assertEqual((rc, out), (0, "one\n42\nthree\n"), err)

    def test_unselected_multi_block_error_mentions_all_blocks(self):
        rc, _, err = cli(self.DOC, "run", suffix=".md")
        self.assertEqual(rc, 65)
        self.assertIn("--all-blocks", err)

    def test_stops_at_first_nonzero_exit(self):
        doc = ("```mermaid\nflowchart TD\n a[[\"echo first; exit 4\"]]\n```\n"
               "```mermaid\nflowchart TD\n b[\"'never'\"]\n```\n")
        rc, out, err = cli(doc, "run", "--all-blocks", suffix=".md")
        self.assertEqual((rc, out), (4, "first\n"))
        self.assertIn("skipping the remaining 1 block", err)

    def test_runtime_error_in_later_block_stops_run(self):
        doc = ("```mermaid\nflowchart TD\n a[\"'one'\"]\n```\n"
               "```mermaid\nflowchart TD\n b[\"1/0\"]\n```\n"
               "```mermaid\nflowchart TD\n c[\"'never'\"]\n```\n")
        rc, out, err = cli(doc, "run", "--all-blocks", suffix=".md")
        self.assertEqual((rc, out), (70, "one\n"))
        self.assertIn("ZeroDivisionError", err)

    def test_all_blocks_are_linted_before_any_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "touched")
            doc = (f"```mermaid\nflowchart TD\n a[[\"touch {marker}\"]]\n```\n"
                   "```mermaid\nflowchart TD\n b --> c\n b --> d\n```\n")
            rc, _, err = cli(doc, "run", "--all-blocks", suffix=".md")
            self.assertEqual(rc, 65)
            self.assertIn("E003", err)
            self.assertFalse(os.path.exists(marker))

    def test_lint_all_blocks_reports_absolute_lines(self):
        doc = ("```mermaid\nflowchart TD\n a[\"1\"]\n```\n"
               "```mermaid\nflowchart TD\n b --> c\n b --> d\n```\n")
        rc, out, _ = cli(doc, "lint", "--all-blocks", suffix=".md")
        self.assertEqual(rc, 65)
        self.assertIn(":8: error E003", out)

    def test_names_bound_in_earlier_blocks_are_known_to_later_templates(self):
        doc = ("```mermaid\nflowchart TD\n a[\"who := 'Ada'\"]\n```\n"
               "```mermaid\nflowchart TD\n b{{\"hi ${who}\"}}\n```\n")
        rc, out, _ = cli(doc, "lint", "--all-blocks", suffix=".md")
        self.assertEqual((rc, out.strip()), (0, "ok"))

    def test_conflict_with_block_and_dry_run_and_plain_file(self):
        rc, _, _ = cli(self.DOC, "run", "--all-blocks", "--block", "1", suffix=".md")
        self.assertEqual(rc, 64)
        rc, out, _ = cli(self.DOC, "run", "--all-blocks", "--dry-run", suffix=".md")
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("== block"), 3)
        rc, out, _ = cli('flowchart TD\n a["1"]\n', "run", "--all-blocks")
        self.assertEqual((rc, out), (0, "1\n"))

    def test_report_lists_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            rep = os.path.join(tmp, "r.json")
            cli(self.DOC, "run", "--all-blocks", "--report", rep, suffix=".md")
            with open(rep) as fh:
                data = json.load(fh)
        self.assertEqual([b["block"] for b in data["blocks"]], [1, 2, 3])
        self.assertEqual(data["output"], "one\n42\nthree\n")
        self.assertEqual({p["block"] for p in data["path"]}, {1, 2, 3})


if __name__ == "__main__":
    unittest.main()
