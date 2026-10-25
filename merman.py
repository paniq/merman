#!/usr/bin/env python3
"""merman: run mermaid flowcharts as programs.

Nodes are Python expressions, shell commands or LLM prompts (routed to the
jerry CLI). The value a node produces selects the outgoing edge. See
README.md for a summary and the specification for the full rules.

Standard library only.
"""
from __future__ import annotations

import argparse
import ast
import bisect
import builtins
import contextlib
import html.entities
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import types
from dataclasses import dataclass, field
from typing import Any, Optional

__version__ = "0.1.0"

EX_OK = 0
EX_USAGE = 64
EX_DATAERR = 65
EX_SOFTWARE = 70
EX_INTERRUPTED = 130

CTX_BUILTINS = ("_", "_out", "_err", "_rc", "_steps")


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class MermanError(Exception):
    """An error with a stable code (E... static, R... runtime)."""

    def __init__(self, code: str, message: str, node: Optional[str] = None,
                 line: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.node = node
        self.line = line


class ParseError(MermanError):
    def __init__(self, message: str, line: Optional[int] = None):
        super().__init__("E001", message, line=line)


class LabelError(ValueError):
    """A malformed edge label (lint code E005)."""


class NodeTimeout(BaseException):
    """Raised by the alarm handler; BaseException so user code can't swallow it."""


class Interrupted(Exception):
    pass


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------

_ENTITY = re.compile(r"#(\w+);")
_BR = re.compile(r"<br\s*/?>", re.I)
_NUM = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")


def decode_text(s: str) -> str:
    """Mermaid text: <br/> becomes a newline, #quot; style entities are decoded."""
    s = _BR.sub("\n", s)

    def rep(m: re.Match) -> str:
        name = m.group(1)
        if name.isdigit():
            cp = int(name)
            return chr(cp) if cp < 0x110000 else m.group(0)
        cp2 = html.entities.name2codepoint.get(name)
        return chr(cp2) if cp2 is not None else m.group(0)

    return _ENTITY.sub(rep, s)


def clean_text(s: str) -> str:
    return decode_text(s).strip()


def as_number(s: str) -> Optional[float]:
    s = s.strip()
    return float(s) if _NUM.match(s) else None


def shorten(s: str, n: int = 160) -> str:
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[: n - 1] + "…"


# --------------------------------------------------------------------------
# label matchers (spec section 4)
# --------------------------------------------------------------------------


class Matcher:
    def matches(self, s: str) -> bool:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError

    def interval(self):
        """(numeric, lo, lo_closed, hi, hi_closed) or None. Used for overlap lint."""
        return None


class Default(Matcher):
    def matches(self, s: str) -> bool:  # handled by select_edge
        return False

    def describe(self) -> str:
        return "anything else"


@dataclass
class Equal(Matcher):
    text: str

    def matches(self, s: str) -> bool:
        return s == self.text

    def describe(self) -> str:
        return self.text


@dataclass
class Regex(Matcher):
    src: str
    flags: str
    rx: re.Pattern

    def matches(self, s: str) -> bool:
        return self.rx.search(s) is not None

    def describe(self) -> str:
        return f"text matching the regular expression /{self.src}/{self.flags}"


@dataclass
class Glob(Matcher):
    pattern: str
    rx: re.Pattern

    def matches(self, s: str) -> bool:
        return self.rx.fullmatch(s) is not None

    def describe(self) -> str:
        return f"text matching the pattern {self.pattern}"


def describe_interval(numeric: bool, lo, lo_closed, hi, hi_closed) -> str:
    """Plain-language description of an interval; lo/hi are display strings or None."""
    if numeric:
        if lo is not None and hi is not None:
            if lo_closed and hi_closed:
                return f"a number from {lo} to {hi} (inclusive)"
            if lo_closed:
                return f"a number from {lo} up to but not including {hi}"
            if hi_closed:
                return f"a number greater than {lo} and up to {hi}"
            return f"a number strictly between {lo} and {hi}"
        if lo is not None:
            return f"a number that is {'at least' if lo_closed else 'greater than'} {lo}"
        return f"a number that is {'at most' if hi_closed else 'less than'} {hi}"
    parts = []
    if lo is not None:
        parts.append(f"{'at or after' if lo_closed else 'after'} \"{lo}\"")
    if hi is not None:
        parts.append(f"{'at or before' if hi_closed else 'before'} \"{hi}\"")
    return "text that sorts " + " and ".join(parts)


@dataclass
class Range(Matcher):
    lo: Any
    lo_closed: bool
    hi: Any
    hi_closed: bool
    numeric: bool
    lo_s: Optional[str]
    hi_s: Optional[str]

    def matches(self, s: str) -> bool:
        if self.numeric:
            v = as_number(s)
            if v is None:
                return False
        else:
            v = s
        if self.lo is not None and (v < self.lo or (v == self.lo and not self.lo_closed)):
            return False
        if self.hi is not None and (v > self.hi or (v == self.hi and not self.hi_closed)):
            return False
        return True

    def describe(self) -> str:
        return describe_interval(self.numeric, self.lo_s, self.lo_closed, self.hi_s, self.hi_closed)

    def interval(self):
        return (self.numeric, self.lo, self.lo_closed, self.hi, self.hi_closed)


@dataclass
class Compare(Matcher):
    op: str
    operand: str
    num: Optional[float]

    def matches(self, s: str) -> bool:
        if self.num is not None:
            v = as_number(s)
            if v is None:
                return self.op == "!="
            a, b = v, self.num
        else:
            a, b = s, self.operand
        op = self.op
        if op == "<":
            return a < b
        if op == "<=":
            return a <= b
        if op == ">":
            return a > b
        if op == ">=":
            return a >= b
        return a != b

    def describe(self) -> str:
        if self.op == "!=":
            return f"anything except {self.operand}"
        numeric = self.num is not None
        if self.op in ("<", "<="):
            return describe_interval(numeric, None, False, self.operand, self.op == "<=")
        return describe_interval(numeric, self.operand, self.op == ">=", None, False)

    def interval(self):
        if self.op == "!=":
            return None
        x = self.num if self.num is not None else self.operand
        numeric = self.num is not None
        if self.op == "<":
            return (numeric, None, False, x, False)
        if self.op == "<=":
            return (numeric, None, False, x, True)
        if self.op == ">":
            return (numeric, x, False, None, False)
        return (numeric, x, True, None, False)


_B = r"(?:(?!\.\.)[^\s\[\]()])*"
_RANGE_RE = re.compile(r"([\[(])\s*(" + _B + r")\s*\.\.\s*(" + _B + r")\s*([\])])")
_REGEX_RE = re.compile(r"/(.*)/([ims]*)", re.S)
_CMP_RE = re.compile(r"(<=|>=|!=|<|>)\s*(.*)", re.S)


def glob_to_regex(pattern: str) -> re.Pattern:
    out = []
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return re.compile("".join(out), re.S)


def parse_label(raw: str) -> Matcher:
    """Classify an edge label (spec 4.3). Raises LabelError when malformed."""
    s = raw.strip()
    if not s:
        raise LabelError("empty edge label")
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return Equal(s[1:-1])
    if s in ("else", "*"):
        return Default()
    m = _REGEX_RE.fullmatch(s)
    if m:
        src, fl = m.group(1), m.group(2)
        if not src:
            raise LabelError("empty regular expression")
        flags = 0
        for f in fl:
            flags |= {"i": re.I, "m": re.M, "s": re.S}[f]
        try:
            return Regex(src, fl, re.compile(src, flags))
        except re.error as e:
            raise LabelError(f"invalid regular expression /{src}/: {e}")
    m = _RANGE_RE.fullmatch(s)
    if m:
        opener, lo_s, hi_s, closer = m.groups()
        lo_s = lo_s or None
        hi_s = hi_s or None
        if lo_s is None and hi_s is None:
            raise LabelError("range has no bounds")
        present = [b for b in (lo_s, hi_s) if b is not None]
        numeric = all(as_number(b) is not None for b in present)
        lo = (as_number(lo_s) if numeric else lo_s) if lo_s is not None else None
        hi = (as_number(hi_s) if numeric else hi_s) if hi_s is not None else None
        if lo is not None and hi is not None and lo > hi:
            raise LabelError(f"range lower bound {lo_s} is above upper bound {hi_s}")
        return Range(lo, opener == "[", hi, closer == "]", numeric, lo_s, hi_s)
    m = _CMP_RE.fullmatch(s)
    if m:
        op, operand = m.group(1), m.group(2).strip()
        if not operand:
            raise LabelError(f"comparison '{op}' has no operand")
        return Compare(op, operand, as_number(operand))
    if "*" in s or "?" in s:
        return Glob(s, glob_to_regex(s))
    return Equal(s)


def intervals_overlap(a, b) -> bool:
    na, la, lca, ha, hca = a
    nb, lb, lcb, hb, hcb = b
    if na != nb:
        return False
    if la is None:
        lo, loc = lb, lcb
    elif lb is None:
        lo, loc = la, lca
    elif la > lb:
        lo, loc = la, lca
    elif lb > la:
        lo, loc = lb, lcb
    else:
        lo, loc = la, lca and lcb
    if ha is None:
        hi, hic = hb, hcb
    elif hb is None:
        hi, hic = ha, hca
    elif ha < hb:
        hi, hic = ha, hca
    elif hb < ha:
        hi, hic = hb, hcb
    else:
        hi, hic = ha, hca and hcb
    if lo is None or hi is None:
        return True
    if lo < hi:
        return True
    return lo == hi and loc and hic


# --------------------------------------------------------------------------
# diagram model
# --------------------------------------------------------------------------

BIND_RE = re.compile(r"([A-Za-z_]\w*)\s*:=\s*(.*)", re.S)
PREFIX_RE = re.compile(r"(py|sh|ask)\s*:\s*(.*)", re.S)
PREFIX_KIND = {"py": "python", "sh": "shell", "ask": "llm"}
SHAPE_KIND = {"[[": "shell", "{{": "llm"}


@dataclass
class Node:
    id: str
    line: int
    text: Optional[str] = None
    shape: Optional[str] = None
    kind: str = "python"
    bind: Optional[str] = None
    body: str = ""

    def analyze(self) -> None:
        text = self.text if self.text is not None else self.id
        kind = SHAPE_KIND.get(self.shape or "", "python")
        bind = None
        m = BIND_RE.fullmatch(text.strip())
        if m:
            bind, text = m.group(1), m.group(2)
        text = text.strip()
        m = PREFIX_RE.fullmatch(text)
        if m:
            kind, text = PREFIX_KIND[m.group(1)], m.group(2)
        self.kind, self.bind, self.body = kind, bind, text.strip()


@dataclass
class Edge:
    src: str
    dst: str
    label: Optional[str]
    line: int
    index: int
    matcher: Optional[Matcher] = None
    label_error: Optional[str] = None


@dataclass
class Diagram:
    nodes: dict = field(default_factory=dict)
    edges: list = field(default_factory=list)
    start: Optional[str] = None
    timeout: Optional[float] = None
    node_timeouts: dict = field(default_factory=dict)
    allow_no_exit: bool = False
    unknown_directives: list = field(default_factory=list)
    bad_directives: list = field(default_factory=list)
    subgraph_lines: list = field(default_factory=list)
    _out: Optional[dict] = None

    @property
    def entry(self) -> Optional[str]:
        if self.start:
            return self.start
        return next(iter(self.nodes), None)

    def add_edge(self, src: str, dst: str, label: Optional[str], line: int) -> None:
        self.edges.append(Edge(src, dst, label, line, len(self.edges)))

    def finalize(self) -> None:
        for n in self.nodes.values():
            n.analyze()
        for e in self.edges:
            if e.label is None:
                continue
            try:
                e.matcher = parse_label(e.label)
            except LabelError as ex:
                e.label_error = str(ex)
        self._out = None

    def outgoing(self) -> dict:
        if self._out is None:
            out: dict = {}
            for e in self.edges:
                out.setdefault(e.src, []).append(e)
            self._out = out
        return self._out


# --------------------------------------------------------------------------
# parser (the supported mermaid flowchart subset, spec section 2)
# --------------------------------------------------------------------------

OPENERS = [
    ("(((", ")))"), ("[[", "]]"), ("[(", ")]"), ("([", "])"), ("((", "))"),
    ("{{", "}}"), ("{", "}"), ("(", ")"), ("[", "]"), (">", "]"),
]

_HEADER = re.compile(r"(?:flowchart|graph)(?:-elk)?\b")
_DIRECTION = re.compile(r"[ \t]+(?:TB|TD|BT|RL|LR)\b")
_KEYWORD_LINE = re.compile(
    r"(subgraph|style|classDef|class|linkStyle|click|direction|accTitle|accDescr|title)(?=[ \t])")
_END = re.compile(r"end(?=[ \t]*(?:[\n;]|\Z))")
_ID = re.compile(r"\w+")
_CLASS_SUFFIX = re.compile(r":::[\w-]+")
_ARROW = re.compile(r"-{2,}>|={2,}>|-\.+->")
_UNDIRECTED = re.compile(r"-{3,}(?![>\w])|={3,}(?![>\w])|-\.+-(?![>\w])")
_LABEL_OPEN = re.compile(r"(--|==|-\.)(?=[ \t])")
_CLOSERS = {
    "--": (re.compile(r"-{2,}>"), re.compile(r"\s+-{2,}>")),
    "==": (re.compile(r"={2,}>"), re.compile(r"\s+={2,}>")),
    "-.": (re.compile(r"\.+->"), re.compile(r"\s+\.+->")),
}


@dataclass
class EdgeOp:
    label: Optional[str]


class Parser:
    def __init__(self, text: str, first_line: int = 1):
        self.text = text
        self.n = len(text)
        self.pos = 0
        self.first_line = first_line
        self.d = Diagram()
        self.header = False
        self._starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]

    # -- positions -------------------------------------------------------
    def line(self, pos: Optional[int] = None) -> int:
        p = self.pos if pos is None else pos
        return bisect.bisect_right(self._starts, p) - 1 + self.first_line

    def fail(self, msg: str, pos: Optional[int] = None):
        raise ParseError(msg, self.line(pos))

    def skip_inline(self) -> None:
        while self.pos < self.n and self.text[self.pos] in " \t\r":
            self.pos += 1

    def skip_blank(self) -> None:
        while self.pos < self.n and self.text[self.pos] in " \t\r\n;":
            self.pos += 1

    def skip_to_eol(self) -> None:
        j = self.text.find("\n", self.pos)
        self.pos = self.n if j < 0 else j

    # -- top level -------------------------------------------------------
    def parse(self) -> Diagram:
        m = re.match(r"\s*---[ \t]*\n", self.text)
        if m:
            end = re.search(r"^---[ \t]*$", self.text[m.end():], re.M)
            if end:
                self.pos = m.end() + end.end()
        while True:
            self.skip_blank()
            if self.pos >= self.n:
                break
            if self.text.startswith("%%", self.pos):
                self.comment()
            elif not self.header:
                self.parse_header()
            else:
                self.statement()
        if not self.header:
            raise ParseError("empty diagram: expected a 'flowchart' or 'graph' header", 1)
        self.d.finalize()
        return self.d

    def parse_header(self) -> None:
        m = _HEADER.match(self.text, self.pos)
        if not m:
            self.fail("expected a 'flowchart' or 'graph' header")
        self.pos = m.end()
        m = _DIRECTION.match(self.text, self.pos)
        if m:
            self.pos = m.end()
        self.header = True

    def comment(self) -> None:
        ln = self.line()
        start = self.pos
        self.skip_to_eol()
        m = re.match(r"%%\s*merman:\s*(.*?)\s*$", self.text[start:self.pos])
        if m:
            self.directive(m.group(1), ln)

    def directive(self, body: str, ln: int) -> None:
        toks = body.split()
        if not toks:
            return
        node = None
        if "=" not in toks[0] and toks[0] != "allow-no-exit":
            node, toks = toks[0], toks[1:]
        for tok in toks:
            key, _, val = tok.partition("=")
            if tok == "allow-no-exit" and node is None:
                self.d.allow_no_exit = True
            elif key == "start" and node is None and val:
                self.d.start = val
            elif key == "timeout":
                try:
                    secs = float(val)
                    if secs <= 0:
                        raise ValueError
                except ValueError:
                    self.d.bad_directives.append((ln, f"invalid timeout value {val!r}"))
                    continue
                if node is None:
                    self.d.timeout = secs
                else:
                    self.d.node_timeouts[node] = secs
            else:
                self.d.unknown_directives.append((ln, tok if node is None else f"{node} {tok}"))
        if node is not None and not toks:
            self.d.unknown_directives.append((ln, node))

    def statement(self) -> None:
        t, p = self.text, self.pos
        m = _KEYWORD_LINE.match(t, p)
        if m:
            if m.group(1) == "subgraph":
                self.d.subgraph_lines.append(self.line())
            self.skip_to_eol()
            return
        m = _END.match(t, p)
        if m:
            self.pos = m.end()
            return
        srcs = self.node_group()
        while True:
            self.skip_inline()
            op_pos = self.pos
            op = self.edge_op()
            if op is None:
                break
            dsts = self.node_group()
            ln = self.line(op_pos)
            for s in srcs:
                for dst in dsts:
                    self.d.add_edge(s, dst, op.label, ln)
            srcs = dsts
        self.skip_inline()
        if self.pos >= self.n or self.text[self.pos] in "\n;":
            return
        if self.text.startswith("%%", self.pos):
            return
        self.fail(f"unexpected text {self.text[self.pos:self.pos + 12]!r}")

    # -- nodes -------------------------------------------------------------
    def node_group(self) -> list:
        self.skip_inline()
        ids = [self.node()]
        while True:
            self.skip_inline()
            if self.pos < self.n and self.text[self.pos] == "&":
                self.pos += 1
                self.skip_inline()
                ids.append(self.node())
            else:
                return ids

    def node(self) -> str:
        t = self.text
        m = _ID.match(t, self.pos)
        if not m:
            self.fail(f"expected a node id, found {t[self.pos:self.pos + 12]!r}")
        nid = m.group()
        ln = self.line()
        self.pos = m.end()
        shape = text = None
        for op, cl in OPENERS:
            if t.startswith(op, self.pos):
                self.pos += len(op)
                text = self.shape_text(cl, nid)
                shape = op
                break
        m = _CLASS_SUFFIX.match(t, self.pos)
        if m:
            self.pos = m.end()
        n = self.d.nodes.get(nid)
        if n is None:
            n = self.d.nodes[nid] = Node(nid, ln)
        if text is not None:
            n.text, n.shape = text, shape
        return nid

    def shape_text(self, closer: str, nid: str) -> str:
        t = self.text
        start = self.pos
        while self.pos < self.n and t[self.pos] in " \t\r\n":
            self.pos += 1
        if t.startswith('"', self.pos):
            j = t.find('"', self.pos + 1)
            if j < 0:
                self.fail(f"unterminated quoted text in node {nid}", start)
            raw = t[self.pos + 1:j]
            self.pos = j + 1
            while self.pos < self.n and t[self.pos] in " \t\r\n":
                self.pos += 1
            if not t.startswith(closer, self.pos):
                self.fail(f"expected {closer!r} after quoted text of node {nid}")
            self.pos += len(closer)
        else:
            j = t.find(closer, self.pos)
            if j < 0:
                self.fail(f"node {nid}: missing {closer!r} (quote the text if it contains brackets)", start)
            raw = t[self.pos:j]
            self.pos = j + len(closer)
        return clean_text(raw)

    # -- edges -------------------------------------------------------------
    def edge_op(self) -> Optional[EdgeOp]:
        t, p = self.text, self.pos
        m = _ARROW.match(t, p)
        if m:
            self.pos = m.end()
            self.skip_inline()
            if t.startswith("|", self.pos):
                return EdgeOp(self.pipe_label())
            return EdgeOp(None)
        if _UNDIRECTED.match(t, p):
            self.fail("undirected links ('---') are not supported; use '-->'")
        m = _LABEL_OPEN.match(t, p)
        if m:
            strict, search = _CLOSERS[m.group(1)]
            self.pos = m.end()
            self.skip_inline()
            if t.startswith('"', self.pos):
                j = t.find('"', self.pos + 1)
                if j < 0:
                    self.fail("unterminated quoted edge label", p)
                label = t[self.pos + 1:j]
                self.pos = j + 1
                self.skip_inline()
                c = strict.match(t, self.pos)
                if not c:
                    self.fail("expected an arrow after the edge label")
                self.pos = c.end()
            else:
                c = search.search(t, self.pos)
                if not c:
                    self.fail("unterminated edge label (no closing arrow)", p)
                label = t[self.pos:c.start()]
                self.pos = c.end()
            return EdgeOp(clean_text(label))
        return None

    def pipe_label(self) -> str:
        t = self.text
        self.pos += 1  # the opening '|'
        self.skip_inline()
        if t.startswith('"', self.pos):
            j = t.find('"', self.pos + 1)
            if j < 0:
                self.fail("unterminated quoted edge label")
            label = t[self.pos + 1:j]
            self.pos = j + 1
            self.skip_inline()
            if not t.startswith("|", self.pos):
                self.fail("expected '|' after the quoted edge label")
            self.pos += 1
        else:
            j = t.find("|", self.pos)
            if j < 0:
                self.fail("unterminated edge label (missing closing '|')")
            label = t[self.pos:j]
            self.pos = j + 1
        return clean_text(label)


def parse_diagram(text: str, first_line: int = 1) -> Diagram:
    return Parser(text, first_line).parse()


_FENCE_OPEN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*mermaid\b.*$")


def extract_blocks(text: str) -> list:
    """Fenced ```mermaid blocks as (content, first_line)."""
    lines = text.split("\n")
    blocks, i = [], 0
    while i < len(lines):
        m = _FENCE_OPEN.match(lines[i])
        if not m:
            i += 1
            continue
        fence = m.group(1)
        close = re.compile(r"^[ \t]{0,3}" + re.escape(fence[0]) + "{%d,}[ \t]*$" % len(fence))
        j = i + 1
        while j < len(lines) and not close.match(lines[j]):
            j += 1
        blocks.append(("\n".join(lines[i + 1:j]), i + 2))
        i = j + 1
    return blocks


def load_sources(path: str, block: Optional[int] = None, all_blocks: bool = False) -> list:
    """Return the diagrams to use as [(text, first_line), ...]."""
    if path == "-":
        text = sys.stdin.read()
    else:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    is_md = path.lower().endswith((".md", ".markdown")) or text.lstrip().startswith(("```", "~~~"))
    if not is_md:
        return [(text, 1)]
    blocks = extract_blocks(text)
    if not blocks:
        raise MermanError("E001", f"{path}: no ```mermaid block found")
    if all_blocks:
        return blocks
    if block is None:
        if len(blocks) > 1:
            raise MermanError("E001", f"{path}: {len(blocks)} mermaid blocks found; "
                                      "choose one with --block N or run them all with --all-blocks")
        return [blocks[0]]
    if not 1 <= block <= len(blocks):
        raise MermanError("E001", f"{path}: --block {block} out of range (1..{len(blocks)})")
    return [blocks[block - 1]]


# --------------------------------------------------------------------------
# lint (spec section 7)
# --------------------------------------------------------------------------


@dataclass
class Diagnostic:
    code: str
    message: str
    line: Optional[int] = None

    @property
    def severity(self) -> str:
        return "error" if self.code.startswith("E") else "warning"


# Prompt templates use shell syntax: ${name}, ${name:-default}, ${name-default}; \${ is a literal ${.
_TPL = re.compile(r"\\\$\{|\$\{([A-Za-z_]\w*)(?:(:?-)([^{}]*))?\}")


def declared_names(d: Diagram) -> set:
    """Names a diagram binds: `name :=` bindings and walrus targets in Python nodes."""
    names = set()
    for n in d.nodes.values():
        if n.bind:
            names.add(n.bind)
        if n.kind == "python" and n.body:
            try:
                for sub in ast.walk(ast.parse(n.body, mode="eval")):
                    if isinstance(sub, ast.NamedExpr) and isinstance(sub.target, ast.Name):
                        names.add(sub.target.id)
            except SyntaxError:
                pass
    return names


def lint(d: Diagram, known: Optional[set] = None) -> list:
    known = set(known or ())
    diags: list = []

    def add(code, msg, line=None):
        diags.append(Diagnostic(code, msg, line))

    if not d.nodes:
        add("E001", "the diagram has no nodes", 1)
        return diags
    for ln, msg in d.bad_directives:
        add("E008", msg, ln)
    for ln, tok in d.unknown_directives:
        add("W009", f"unknown merman directive {tok!r}", ln)
    for node_id in d.node_timeouts:
        if node_id not in d.nodes:
            add("E008", f"timeout directive names unknown node {node_id!r}")
    if d.start and d.start not in d.nodes:
        add("E007", f"start node {d.start!r} does not exist")
    for ln in d.subgraph_lines:
        add("W008", "subgraph flattened (its grouping is ignored)", ln)

    bound = set(known) | set(CTX_BUILTINS) | declared_names(d)

    for n in d.nodes.values():
        if n.text is None:
            add("W010", f"node {n.id} has no text; its id is used as the body", n.line)
        if not n.body:
            add("E006", f"node {n.id} has an empty body", n.line)
            continue
        if n.kind == "python":
            try:
                compile(n.body, f"<{n.id}>", "eval")
            except SyntaxError as e:
                add("E009", f"node {n.id}: not a valid Python expression ({e.msg})", n.line)
        elif n.kind == "llm":
            for m in _TPL.finditer(n.body):
                name = m.group(1)
                if name and m.group(2) is None and name not in bound:
                    add("W007", f"node {n.id}: template variable ${{{name}}} is never set", n.line)

    for nid, es in d.outgoing().items():
        node_line = d.nodes[nid].line if nid in d.nodes else None
        labeled = [e for e in es if e.label is not None]
        unlabeled = [e for e in es if e.label is None]
        if labeled and unlabeled:
            add("E002", f"node {nid} mixes labeled and unlabeled outgoing edges", unlabeled[0].line)
        if len(unlabeled) > 1:
            add("E003", f"node {nid} has {len(unlabeled)} unlabeled outgoing edges", unlabeled[1].line)
        for e in labeled:
            if e.label_error:
                add("E005", f"edge {e.src} --> {e.dst}: {e.label_error}", e.line)
        good = [e for e in labeled if e.matcher is not None]
        defaults = [e for e in good if isinstance(e.matcher, Default)]
        if len(defaults) > 1:
            add("E004", f"node {nid} has {len(defaults)} default edges", defaults[1].line)
        if labeled and not defaults and len(good) == len(labeled):
            add("W006", f"node {nid} branches without a default edge (no match is a runtime error)", node_line)
        seen: dict = {}
        for j, e in enumerate(good):
            m = e.matcher
            if isinstance(m, Equal):
                if m.text in seen:
                    add("W003", f"node {nid}: duplicate label {m.text!r}; the later edge never fires", e.line)
                    continue
                seen[m.text] = e
                for earlier in good[:j]:
                    em = earlier.matcher
                    if isinstance(em, (Glob, Range, Compare)) and em.matches(m.text):
                        add("W004", f"node {nid}: label {m.text!r} is shadowed by the earlier label {earlier.label!r}", e.line)
                        break
        iv = [(e, e.matcher.interval()) for e in good if e.matcher.interval() is not None]
        for a in range(len(iv)):
            for b in range(a + 1, len(iv)):
                if intervals_overlap(iv[a][1], iv[b][1]):
                    add("W005", f"node {nid}: labels {iv[a][0].label!r} and {iv[b][0].label!r} overlap (the first match wins)", iv[b][0].line)

    entry = d.entry
    if entry in d.nodes:
        out = d.outgoing()
        seen_nodes, stack = {entry}, [entry]
        while stack:
            cur = stack.pop()
            for e in out.get(cur, []):
                if e.dst not in seen_nodes:
                    seen_nodes.add(e.dst)
                    stack.append(e.dst)
        for n in d.nodes.values():
            if n.id not in seen_nodes:
                add("W002", f"node {n.id} is unreachable from the entry node {entry}", n.line)
        if not d.allow_no_exit and not any(not out.get(x) for x in seen_nodes):
            add("W001", "no exit path: no node without outgoing edges is reachable from the entry", d.nodes[entry].line)

    diags.sort(key=lambda x: (x.line or 0, x.code))
    return diags


# --------------------------------------------------------------------------
# execution (spec sections 3, 5, 9)
# --------------------------------------------------------------------------

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def select_edge(edges: list, value: Any) -> Optional[Edge]:
    """Follow a consequence, else the first matching label in declaration order, else the default."""
    if not edges:
        return None
    if len(edges) == 1 and edges[0].label is None:
        return edges[0]
    s = str(value)
    default = None
    for e in edges:
        m = e.matcher
        if m is None:
            continue
        if isinstance(m, Default):
            if default is None:
                default = e
            continue
        if m.matches(s):
            return e
    return default


def render_template(body: str, ctx: dict) -> str:
    """Substitute ${name}, ${name:-default} (unset or empty) and ${name-default} (unset only)."""
    def rep(m: re.Match) -> str:
        if m.group(0) == "\\${":
            return "${"
        name, op, default = m.group(1), m.group(2), m.group(3)
        if name in ctx:
            value = str(ctx[name])
            if not (op == ":-" and value == ""):
                return value
        if op is not None:
            return default
        raise KeyError(name)

    return _TPL.sub(rep, body)


def build_system_message(edges: list) -> str:
    """The answer-format block appended to the prompt (spec section 9)."""
    literals: list = []
    descs: list = []
    has_default = False
    for e in edges:
        m = e.matcher
        if isinstance(m, Default):
            has_default = True
        elif isinstance(m, Equal):
            if m.text not in literals:
                literals.append(m.text)
        elif m is not None:
            descs.append(m.describe())
    lines = []
    if literals:
        lines.append("Reply with exactly one of these answers:")
        lines += [f"- {t}" for t in literals]
        lines += [f"Or, if it fits instead: {d}." for d in descs]
    elif descs:
        lines.append("Reply with an answer of one of these kinds:")
        lines += [f"- {d}" for d in descs]
    if has_default and (literals or descs):
        lines.append("If none of these fit, reply with a short answer of your own.")
    lines.append("Answer only, with no explanation, quotes or trailing punctuation.")
    return "\n".join(lines)


@contextlib.contextmanager
def time_limit(seconds: Optional[float]):
    usable = (seconds and hasattr(signal, "setitimer")
              and threading.current_thread() is threading.main_thread())
    if not usable:
        yield
        return

    def handler(signum, frame):
        raise NodeTimeout()

    old = signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


@dataclass
class Options:
    max_steps: int = 1000
    timeout: Optional[float] = None
    echo: bool = False
    trace: bool = False
    jerry: Optional[str] = None
    llm_args: list = field(default_factory=list)
    llm_retries: int = 0
    stdin_consumed: bool = False


@dataclass
class Outcome:
    value: Any
    stdout: str = ""
    stderr: str = ""
    bound: Any = None


class Run:
    def __init__(self, diagram: Diagram, ctx: dict, opts: Options, out=None, err=None):
        self.d = diagram
        self.ctx = ctx
        self.opts = opts
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.path: list = []
        self.output: list = []
        self.exit_code: Optional[int] = None
        self.ctx.setdefault("__builtins__", builtins)
        for k, v in (("_", None), ("_out", ""), ("_err", ""), ("_rc", None), ("_steps", 0)):
            self.ctx.setdefault(k, v)

    # -- helpers -----------------------------------------------------------
    def emit(self, text: str) -> None:
        self.output.append(text)
        self.out.write(text)
        self.out.flush()

    def exports(self) -> dict:
        env = {}
        for k, v in self.ctx.items():
            if not isinstance(k, str) or k.startswith("__") or not _ENV_NAME.match(k):
                continue
            if callable(v) or isinstance(v, types.ModuleType):
                continue
            env[k] = str(v).replace("\0", "")
        return env

    def timeout_for(self, node: Node) -> Optional[float]:
        if node.id in self.d.node_timeouts:
            return self.d.node_timeouts[node.id]
        if self.opts.timeout is not None:
            return self.opts.timeout
        return self.d.timeout

    def public_context(self) -> dict:
        return {k: v for k, v in self.ctx.items() if isinstance(k, str) and not k.startswith("__")
                and not callable(v) and not isinstance(v, types.ModuleType)}

    # -- node kinds ----------------------------------------------------------
    def exec_python(self, node: Node, timeout) -> Outcome:
        try:
            code = compile(node.body, f"<{node.id}>", "eval")
            with time_limit(timeout):
                value = eval(code, self.ctx)
        except NodeTimeout:
            raise MermanError("R004", f"python node timed out after {timeout:g}s", node.id)
        except Exception as e:
            raise MermanError("R002", f"{type(e).__name__}: {e}", node.id)
        return Outcome(value, bound=value)

    def exec_shell(self, node: Node, timeout) -> Outcome:
        env = os.environ.copy()
        env.update(self.exports())
        try:
            proc = subprocess.Popen(
                node.body, shell=True, env=env, start_new_session=True,
                stdin=subprocess.DEVNULL if self.opts.stdin_consumed else None,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            raise MermanError("R002", f"could not start shell command: {e}", node.id)
        out_buf: list = []
        err_buf: list = []

        def read_out():
            out_buf.append(proc.stdout.read())

        def tee_err():
            for line in iter(proc.stderr.readline, b""):
                text = line.decode("utf-8", "replace")
                err_buf.append(text)
                self.err.write(text)
                self.err.flush()

        threads = [threading.Thread(target=read_out, daemon=True),
                   threading.Thread(target=tee_err, daemon=True)]
        for t in threads:
            t.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        except KeyboardInterrupt:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise
        for t in threads:
            t.join(2)
        if timed_out:
            raise MermanError("R004", f"shell node timed out after {timeout:g}s", node.id)
        rc = proc.returncode
        if rc < 0:
            rc = 128 - rc
        stdout = (out_buf[0] if out_buf else b"").decode("utf-8", "replace")
        stderr = "".join(err_buf)
        return Outcome(rc, stdout=stdout, stderr=stderr, bound=stdout.strip())

    def call_jerry(self, node: Node, prompt: str, system: Optional[str], seed: int, timeout) -> str:
        exe = self.opts.jerry or os.environ.get("MERMAN_JERRY") or "jerry"
        trace = self.opts.trace
        cmd = [exe]
        if trace:
            cmd.append("-r")  # the model's reasoning goes to stderr
        cmd += ["-s", str(seed)]
        if timeout:
            cmd += ["-t", str(math.ceil(timeout))]
        cmd += list(self.opts.llm_args)
        # jerry sends exactly one message, so the answer format is appended to the prompt
        # (its -S is a flag that makes that single message a system message, not an option).
        message = prompt if not system else f"{prompt}\n\n{system}"
        try:
            p = subprocess.run(cmd, input=message.encode("utf-8"), capture_output=True,
                               timeout=(timeout + 1) if timeout else None)
        except FileNotFoundError:
            raise MermanError("R002", f"could not run jerry ({exe!r} not found); "
                                      "install it or use --jerry PATH / MERMAN_JERRY", node.id)
        except subprocess.TimeoutExpired:
            raise MermanError("R004", f"LLM node timed out after {timeout:g}s", node.id)
        except OSError as e:
            raise MermanError("R002", f"could not run jerry: {e}", node.id)
        if p.returncode in (130, -2):
            raise Interrupted()
        detail = p.stderr.decode("utf-8", "replace").strip()
        response = p.stdout.decode("utf-8", "replace").strip()
        if trace:
            self.trace_llm(node, seed, system, prompt, detail, response, p.returncode)
        elif detail and p.returncode == 0:
            # e.g. reasoning from --llm-arg=-r; failures report it in the error message instead
            self.err.write(detail + "\n")
            self.err.flush()
        if p.returncode != 0:
            raise MermanError("R002", f"jerry exited with code {p.returncode}"
                                      + (f": {detail}" if detail else ""), node.id)
        return response

    def trace_llm(self, node, seed, system, prompt, reasoning, response, rc) -> None:
        def block(label: str, text: str) -> None:
            for i, line in enumerate(text.split("\n")):
                self.err.write(f"merman: trace:   {label if i == 0 else ' ' * len(label)}  {line}\n")

        self.err.write(f"merman: trace: {node.id}: asking jerry (seed {seed}, exit code {rc})\n")
        block("prompt>", prompt)
        if system:
            block("format>", system)
        if reasoning:
            block("jerry>", reasoning)
        self.err.write(f"merman: trace:   response> {response!r}\n")
        self.err.flush()

    def exec_llm(self, node: Node, edges: list, timeout) -> Outcome:
        try:
            prompt = render_template(node.body, self.ctx)
        except KeyError as e:
            raise MermanError("R002", f"prompt uses undefined variable ${{{e.args[0]}}}", node.id)
        labeled = [e for e in edges if e.matcher is not None]
        system = build_system_message(labeled) if labeled else None
        has_default = any(isinstance(e.matcher, Default) for e in labeled)
        attempts = 1 + (self.opts.llm_retries if labeled and not has_default else 0)
        resp = ""
        for k in range(attempts):
            resp = self.call_jerry(node, prompt, system, k, timeout)
            if select_edge(edges, resp) is not None:
                break
        return Outcome(resp, bound=resp)

    def execute(self, node: Node, edges: list) -> Outcome:
        timeout = self.timeout_for(node)
        if node.kind == "python":
            return self.exec_python(node, timeout)
        if node.kind == "shell":
            return self.exec_shell(node, timeout)
        return self.exec_llm(node, edges, timeout)

    # -- main loop -------------------------------------------------------------
    def go(self) -> int:
        d = self.d
        outs = d.outgoing()
        nid = d.entry
        steps = 0
        while True:
            node = d.nodes[nid]
            edges = outs.get(nid, [])
            if steps >= self.opts.max_steps:
                raise MermanError("R003", f"step limit of {self.opts.max_steps} exceeded", node.id)
            steps += 1
            self.ctx["_steps"] = steps
            try:
                res = self.execute(node, edges)
            except MermanError as e:
                self.path.append({"node": node.id, "kind": node.kind, "result": None, "edge": None})
                e.node = e.node or node.id
                raise
            self.ctx["_"] = res.value
            if node.kind == "shell":
                self.ctx["_out"] = res.stdout.strip()
                self.ctx["_err"] = res.stderr.strip()
                self.ctx["_rc"] = res.value
            if node.bind:
                self.ctx[node.bind] = res.bound
            entry = {"node": node.id, "kind": node.kind, "result": str(res.value), "edge": None}
            self.path.append(entry)
            if not edges:  # terminal node: its result is the program's result
                if node.kind == "shell":
                    self.emit(res.stdout)
                    self.exit_code = res.value
                else:
                    self.emit(str(res.value) + "\n")
                    self.exit_code = 0
                self.trace_line(len(self.path), node, res, None, None)
                return self.exit_code
            edge = select_edge(edges, res.value)
            if edge is None:
                self.trace_line(len(self.path), node, res, None, None)
                raise MermanError(
                    "R001", f"result {shorten(str(res.value), 80)!r} matched no edge and the node has no default edge",
                    node.id)
            entry["edge"] = edge.label
            if self.opts.echo:
                self.emit(res.stdout if node.kind == "shell" else str(res.value) + "\n")
            self.trace_line(len(self.path), node, res, edge, edge.dst)
            nid = edge.dst

    def trace_line(self, n, node, res, edge, nxt) -> None:
        if not self.opts.trace:
            return
        via = "(end)" if edge is None and nxt is None else f"[{edge.label if edge.label is not None else 'consequence'}] -> {nxt}"
        self.err.write(f"merman: trace {n}: {node.id} ({node.kind}) = {shorten(str(res.value), 60)!r} {via}\n")
        self.err.flush()


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------


class Parser_(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write(f"merman: {message}\n")
        raise SystemExit(EX_USAGE)


def build_cli() -> argparse.ArgumentParser:
    ap = Parser_(prog="merman", description="Run mermaid flowcharts as programs.")
    ap.add_argument("--version", action="version", version=f"merman {__version__}")
    sub = ap.add_subparsers(dest="cmd", parser_class=Parser_)

    def common(p):
        p.add_argument("file", help="a .mmd file, a .md file with a ```mermaid block, or - for stdin")
        p.add_argument("--block", type=int, metavar="N", help="use the Nth ```mermaid block (1-based)")
        p.add_argument("--all-blocks", action="store_true",
                       help="use every ```mermaid block, in order (run: context is shared, "
                            "stops at the first non-zero exit)")
        p.add_argument("--ignore", action="append", default=[], metavar="CODE",
                       help="suppress a warning code (repeatable)")

    r = sub.add_parser("run", help="execute a diagram")
    common(r)
    r.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="set a context variable (string)")
    r.add_argument("--stdin-json", action="store_true", help="read a JSON object from stdin into the context")
    r.add_argument("--max-steps", type=int, default=1000, metavar="N")
    r.add_argument("--timeout", type=float, metavar="S", help="default per-node timeout in seconds")
    r.add_argument("--echo", action="store_true", help="print every node's result as it runs")
    r.add_argument("--trace", action="store_true", help="print each step to stderr")
    r.add_argument("--report", metavar="FILE", help="write a JSON report (outcome, path, final context)")
    r.add_argument("--dry-run", action="store_true", help="parse, lint and list the nodes; execute nothing")
    r.add_argument("--jerry", metavar="PATH", help="the jerry executable (default: jerry on PATH or $MERMAN_JERRY)")
    r.add_argument("--llm-arg", action="append", default=[], metavar="ARG",
                   help="extra jerry arguments (shell-split; repeatable)")
    r.add_argument("--llm-retries", type=int, default=0, metavar="N",
                   help="re-ask an LLM node with no default edge up to N more times (seeds 1..N)")

    li = sub.add_parser("lint", help="check a diagram without executing it")
    common(li)
    li.add_argument("--strict", action="store_true", help="treat warnings as errors")
    li.add_argument("--set", action="append", default=[], metavar="NAME[=VALUE]",
                    help="declare a variable that will be set at run time")
    return ap


def read_diagrams(args) -> list:
    """Parse the selected block(s): [(Diagram, first_line), ...]."""
    if args.all_blocks and args.block is not None:
        sys.stderr.write("merman: --block and --all-blocks are mutually exclusive\n")
        raise SystemExit(EX_USAGE)
    return [(parse_diagram(t, first), first) for t, first in load_sources(args.file, args.block, args.all_blocks)]


def check_ignore(codes: list) -> set:
    bad = [c for c in codes if not re.fullmatch(r"W\d{3}", c)]
    if bad:
        sys.stderr.write(f"merman: only warning codes (W###) can be ignored, got {', '.join(bad)}\n")
        raise SystemExit(EX_USAGE)
    return set(codes)


def print_diags(diags: list, path: str, stream) -> None:
    for g in diags:
        loc = f"{path}:{g.line}" if g.line else path
        stream.write(f"{loc}: {g.severity} {g.code}: {g.message}\n")


def cmd_lint(args) -> int:
    ignore = check_ignore(args.ignore)
    known = {s.partition("=")[0] for s in args.set}
    try:
        diagrams = read_diagrams(args)
    except OSError as e:
        sys.stderr.write(f"merman: {e}\n")
        return EX_USAGE
    except MermanError as e:
        print_diags([Diagnostic(e.code, e.message, e.line)], args.file, sys.stdout)
        return EX_DATAERR
    diags: list = []
    for d, _ in diagrams:
        diags += [g for g in lint(d, known) if g.code not in ignore]
        known |= declared_names(d)
    print_diags(diags, args.file, sys.stdout)
    errors = [g for g in diags if g.severity == "error"]
    if errors or (args.strict and diags):
        return EX_DATAERR
    if not diags:
        print("ok")
    return EX_OK


def dry_run(d: Diagram, out) -> None:
    outs = d.outgoing()
    out.write(f"entry: {d.entry}\n")
    for n in d.nodes.values():
        body = n.body.replace("\n", "\\n")
        bind = f"{n.bind} := " if n.bind else ""
        out.write(f"{n.id}\t{n.kind}\t{bind}{body}\n")
        for e in outs.get(n.id, []):
            out.write(f"    -> {e.dst}" + (f"  [{e.label}]" if e.label is not None else "") + "\n")


def write_report(path: str, code: int, runs: list, firsts: list, err: Optional[MermanError]) -> None:
    steps = []
    for i, r in enumerate(runs, 1):
        steps += [dict(p, block=i) for p in r.path]
    rep = {
        "outcome": "ok" if err is None and code == 0 else ("error" if err else "exit"),
        "exit_code": code,
        "output": "".join("".join(r.output) for r in runs),
        "error": ({"code": err.code, "message": err.message, "node": err.node} if err else None),
        "path": steps,
        "blocks": [{"block": i, "first_line": firsts[i - 1] if i <= len(firsts) else None,
                    "exit_code": r.exit_code} for i, r in enumerate(runs, 1)],
        "context": runs[-1].public_context() if runs else {},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2, default=repr)
        f.write("\n")


def cmd_run(args) -> int:
    ignore = check_ignore(args.ignore)
    ctx: dict = {}
    if args.stdin_json:
        if args.file == "-":
            sys.stderr.write("merman: --stdin-json cannot be combined with reading the diagram from stdin\n")
            return EX_USAGE
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError as e:
            sys.stderr.write(f"merman: --stdin-json: invalid JSON: {e}\n")
            return EX_USAGE
        if not isinstance(data, dict):
            sys.stderr.write("merman: --stdin-json: expected a JSON object\n")
            return EX_USAGE
        ctx.update(data)
    for item in args.set:
        name, eq, value = item.partition("=")
        if not eq or not name.isidentifier():
            sys.stderr.write(f"merman: --set expects NAME=VALUE with an identifier name, got {item!r}\n")
            return EX_USAGE
        ctx[name] = value

    opts = Options(max_steps=args.max_steps, timeout=args.timeout, echo=args.echo, trace=args.trace,
                   jerry=args.jerry, llm_retries=args.llm_retries,
                   stdin_consumed=args.stdin_json)
    for a in args.llm_arg:
        opts.llm_args += shlex.split(a)

    runs: list = []
    firsts: list = []
    err: Optional[MermanError] = None
    code = EX_OK
    try:
        try:
            diagrams = read_diagrams(args)
        except OSError as e:
            sys.stderr.write(f"merman: {e}\n")
            return EX_USAGE
        firsts = [first for _, first in diagrams]
        known = set(ctx)
        errors: list = []
        for d, _ in diagrams:
            diags = [g for g in lint(d, known) if g.code not in ignore]
            known |= declared_names(d)
            print_diags([g for g in diags if g.severity == "warning"], args.file, sys.stderr)
            errors += [g for g in diags if g.severity == "error"]
        if errors:
            print_diags(errors, args.file, sys.stderr)
            err = MermanError(errors[0].code, errors[0].message, line=errors[0].line)
            code = EX_DATAERR
            return code
        if args.dry_run:
            for i, (d, first) in enumerate(diagrams, 1):
                if len(diagrams) > 1:
                    sys.stdout.write(f"== block {i} (line {first})\n")
                dry_run(d, sys.stdout)
            return EX_OK
        for i, (d, _) in enumerate(diagrams, 1):
            run = Run(d, ctx, opts)  # one shared context: later blocks see earlier bindings
            runs.append(run)
            code = run.go()
            if code != 0:
                if i < len(diagrams):
                    sys.stderr.write(f"merman: block {i} ended with exit code {code}; "
                                     f"skipping the remaining {len(diagrams) - i} block(s)\n")
                break
        return code
    except MermanError as e:
        err = e
        if not runs:  # parse failure
            print_diags([Diagnostic(e.code, e.message, e.line)], args.file, sys.stderr)
            code = EX_DATAERR
        else:
            where = f"node {e.node!r}: " if e.node else ""
            sys.stderr.write(f"merman: {e.code}: {where}{e.message}\n")
            if runs[-1].path:
                sys.stderr.write("  path: " + " -> ".join(p["node"] for p in runs[-1].path) + "\n")
            code = EX_SOFTWARE
        return code
    except (KeyboardInterrupt, Interrupted):
        code = EX_INTERRUPTED
        return code
    finally:
        if args.report:
            with contextlib.suppress(OSError):
                write_report(args.report, code, runs, firsts, err)


def main(argv: Optional[list] = None) -> int:
    ap = build_cli()
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else EX_USAGE
    if args.cmd is None:
        ap.print_usage(sys.stderr)
        return EX_USAGE
    try:
        if args.cmd == "lint":
            return cmd_lint(args)
        return cmd_run(args)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else EX_USAGE
    except BrokenPipeError:
        return EX_OK
    except MermanError as e:
        sys.stderr.write(f"merman: {e.code}: {e.message}\n")
        return EX_DATAERR


if __name__ == "__main__":
    sys.exit(main())
