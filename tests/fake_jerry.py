#!/usr/bin/env python3
"""A stand-in for the jerry CLI, used by the tests.

It mimics the parts of the real CLI that matter to merman: -S is a flag (not an
option with a value), the text comes from the positional arguments if there are
any and from stdin otherwise, and exactly one message is sent, as role "system"
with -S and "user" without it.

Answers come from FAKE_JERRY_ANSWERS (comma separated), indexed by the -s seed
(the last one repeats). FAKE_JERRY_LOG appends a JSON line per call.
FAKE_JERRY_EXIT makes it fail with that exit code.
"""
import argparse
import json
import os
import sys

ap = argparse.ArgumentParser()
ap.add_argument("prompt", nargs="*")
for flag in ("-v", "-r", "-f", "-b", "-e", "-S", "-q", "--no-stream"):
    ap.add_argument(flag, action="store_true")
ap.add_argument("-s", "--seed", type=int, default=0)
for opt in ("-i", "-o", "-u", "-m", "-t", "--limit", "--max-turns", "-L"):
    ap.add_argument(opt)
ap.add_argument("-T", action="append")
args, _ = ap.parse_known_args()
argv = sys.argv[1:]

if args.prompt:
    text = " ".join(args.prompt)
else:
    text = sys.stdin.read()
log = os.environ.get("FAKE_JERRY_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"args": argv, "text": text,
                            "role": "system" if args.S else "user"}) + "\n")
code = int(os.environ.get("FAKE_JERRY_EXIT", "0"))
if code:
    sys.stderr.write("fake jerry failure\n")
    sys.exit(code)
if args.r:
    sys.stderr.write("fake reasoning\n")
answers = os.environ.get("FAKE_JERRY_ANSWERS", "ok").split(",")
print(answers[min(args.seed, len(answers) - 1)])
