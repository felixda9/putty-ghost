#!/usr/bin/env python3
"""
ghost.py -- "ghost typer" for a PuTTY serial session to a Cisco device.

You open PuTTY on the console port and watch. This script types INTO your
PuTTY window (so every keystroke is visible on screen, exactly as if you had
typed it) and reads the router's replies back out of PuTTY's session log.

  keystrokes  --PostMessage(WM_CHAR)-->  PuTTY window  --serial-->  router
  router output  --> PuTTY terminal --> session log file --> this script

A COM port is exclusive, so this is the only way to drive the router while
PuTTY still owns the port. Nothing here opens the serial port itself.
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import re
import shutil
import sys
import time
import winreg
from datetime import datetime

# Reuse the tested prompt patterns / output cleaner.
from ciscocon import (clean, P_USER, P_PRIV, P_CONFIG, P_MORE, P_USERNAME,
                      P_PASSWORD, P_INITIAL, P_RETURN, P_CONFIRM, P_SAVEQ,
                      P_IOSERR, find_ports, autodetect)

user32 = ctypes.WinDLL("user32", use_last_error=True)

WM_CHAR = 0x0102
SESSION_NAME = "cisco-console"

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.join(HERE, "logs", "putty-session.log")


def find_putty():
    """Locate putty.exe: $PUTTY, the usual install dirs, then PATH."""
    env = os.environ.get("PUTTY")
    if env and os.path.exists(env):
        return env
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")):
        if not base:
            continue
        cand = os.path.join(base, "PuTTY", "putty.exe")
        if os.path.exists(cand):
            return cand
    found = shutil.which("putty")
    if found:
        return found
    sys.exit("putty.exe not found. Install PuTTY, or set the PUTTY "
             "environment variable to its full path.")

# PuTTY config enums (from putty.h / settings.c)
LGTYP_NONE, LGTYP_ASCII, LGTYP_DEBUG = 0, 1, 2   # DEBUG = "all session output"
FORCE_ON, FORCE_OFF, AUTO = 0, 1, 2


# --- window discovery ------------------------------------------------------
def find_putty_windows(match=None):
    """Every visible top-level window of PuTTY's window class."""
    results = []
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if buf.value != "PuTTY":
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        tbuf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, tbuf, n + 1)
        title = tbuf.value
        if match and match.lower() not in title.lower():
            return True
        results.append((hwnd, title))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return results


def pick_window(match=None):
    wins = find_putty_windows(match)
    if not wins:
        sys.exit("No PuTTY window found."
                 + (" (filter: %r)" % match if match else "")
                 + "\nOpen your serial session first, or run:  "
                   "python ghost.py launch")
    if len(wins) > 1:
        lines = ["Multiple PuTTY windows open -- narrow it with --window:"]
        lines += ["  [%d] %s" % (h, t) for h, t in wins]
        sys.exit("\n".join(lines))
    return wins[0]


# --- saved-session setup ---------------------------------------------------
def setup_session(port, baud, logfile, name=SESSION_NAME, local_echo=None):
    """Create/refresh a PuTTY saved session: serial + logging preconfigured."""
    key = r"SOFTWARE\SimonTatham\PuTTY\Sessions\%s" % name
    os.makedirs(os.path.dirname(logfile), exist_ok=True)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
        def s(n, v):
            winreg.SetValueEx(k, n, 0, winreg.REG_SZ, v)

        def d(n, v):
            winreg.SetValueEx(k, n, 0, winreg.REG_DWORD, v)

        s("Protocol", "serial")
        s("SerialLine", port)
        d("SerialSpeed", baud)
        d("SerialDataBits", 8)
        d("SerialStopHalfbits", 2)     # 2 half-bits == 1 stop bit
        d("SerialParity", 0)           # none
        d("SerialFlowControl", 0)      # none -- required for Cisco console

        # Logging: this is our read-back channel, so it must flush eagerly.
        s("LogFileName", logfile)
        d("LogType", LGTYP_DEBUG)      # all session output
        d("LogFlush", 1)
        d("LogFileClash", 1)           # append; never pop a dialog

        d("ScrollbackLines", 20000)
        d("CloseOnExit", 0)            # keep the window after a disconnect
        if local_echo is not None:
            d("LocalEcho", local_echo)
    return key


def launch(port, baud, logfile, name=SESSION_NAME, fresh_log=True):
    setup_session(port, baud, logfile, name)
    if fresh_log and os.path.exists(logfile):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        os.replace(logfile, logfile + "." + stamp)
    putty = find_putty()
    os.spawnv(os.P_NOWAIT, putty, [putty, "-load", name])
    return logfile


# --- the ghost typer -------------------------------------------------------
class Ghost:
    def __init__(self, hwnd, title, logfile, delay=0.025, timeout=20,
                 verbose=False):
        self.hwnd = hwnd
        self.title = title
        self.logfile = logfile
        self.delay = delay
        self.timeout = timeout
        self.verbose = verbose
        self.pos = 0
        self.hostname = None
        self._seek_end()

    # -- log side --
    def _seek_end(self):
        self.pos = os.path.getsize(self.logfile) if os.path.exists(self.logfile) else 0

    def read_new(self):
        """Bytes appended to the session log since we last looked."""
        if not os.path.exists(self.logfile):
            return ""
        size = os.path.getsize(self.logfile)
        if size < self.pos:          # log rotated/truncated under us
            self.pos = 0
        if size == self.pos:
            return ""
        with open(self.logfile, "rb") as f:
            f.seek(self.pos)
            data = f.read()
            self.pos = f.tell()
        return data.decode("utf-8", "replace")

    def wait_for(self, patterns, timeout=None, send_on_more=True):
        """Poll the log until a pattern matches the tail."""
        timeout = self.timeout if timeout is None else timeout
        deadline = time.time() + timeout
        acc = ""
        while time.time() < deadline:
            chunk = self.read_new()
            if chunk:
                acc += chunk
                if self.verbose:
                    sys.stderr.write(chunk)
                tail = clean(acc)
                if send_on_more and P_MORE.search(tail[-80:]):
                    self.send_raw(" ")     # feed the pager
                    acc += "\n"
                    continue
                for pat in patterns:
                    m = pat.search(tail)
                    if m:
                        return tail, m, pat
            else:
                time.sleep(0.05)
        return clean(acc), None, None

    # -- keyboard side --
    def send_raw(self, text):
        """Post characters to the PuTTY window as if typed."""
        if not user32.IsWindow(self.hwnd):
            sys.exit("The PuTTY window closed.")
        for ch in text:
            if not user32.PostMessageW(self.hwnd, WM_CHAR, ord(ch), 0):
                err = ctypes.get_last_error()
                sys.exit("PostMessage failed (error %d)" % err)
            if self.delay:
                time.sleep(self.delay)

    def send_line(self, line=""):
        self.send_raw(line)
        self.send_raw("\r")

    # -- session ops --
    def probe(self, timeout=8):
        self.send_line("")
        text, m, pat = self.wait_for(
            [P_CONFIG, P_PRIV, P_USER, P_USERNAME, P_PASSWORD,
             P_INITIAL, P_RETURN], timeout)
        if pat is P_CONFIG:
            self.hostname = m.group(1)
            return "config"
        if pat is P_PRIV:
            self.hostname = m.group(1)
            return "privileged"
        if pat is P_USER:
            self.hostname = m.group(1)
            return "user"
        if pat is P_USERNAME:
            return "needs-username"
        if pat is P_PASSWORD:
            return "needs-password"
        if pat is P_INITIAL:
            return "initial-dialog"
        if pat is P_RETURN:
            return "press-return"
        return "no-response"

    def command(self, cmd, timeout=None):
        self.read_new()                      # discard anything stale
        self.send_line(cmd)
        text, m, pat = self.wait_for([P_CONFIG, P_PRIV, P_USER], timeout)
        lines = text.split("\n")
        if lines and cmd.strip() and cmd.strip() in lines[0]:
            lines = lines[1:]                # drop the echoed command
        if lines and re.match(r"^[\w.\-]+(\([^)]*\))?[>#]\s*$", lines[-1]):
            lines = lines[:-1]               # drop the trailing prompt
        return "\n".join(lines).strip("\n"), (pat is not None)

    def config(self, lines, save=False):
        transcript, errors = [], []
        self.read_new()
        self.send_line("configure terminal")
        text, m, pat = self.wait_for([P_CONFIG, P_PRIV, P_USER], 12)
        transcript.append(text)
        if pat is not P_CONFIG:
            return "\n".join(transcript), ["could not enter configuration mode"]

        for line in lines:
            line = line.rstrip()
            if not line or line.lstrip().startswith("!"):
                continue
            out, _ = self.command(line)
            transcript.append(line + ("\n" + out if out else ""))
            if P_IOSERR.search(out):
                errors.append(line + "  ->  " + out.strip().splitlines()[0])

        self.send_line("end")
        text, _, _ = self.wait_for([P_PRIV, P_USER], 12)
        transcript.append(text)

        if save:
            self.send_line("write memory")
            text, m, pat = self.wait_for(
                [P_PRIV, P_USER, P_CONFIRM, P_SAVEQ], 45)
            transcript.append(text)
            if pat in (P_CONFIRM, P_SAVEQ):
                self.send_line("")
                text, _, _ = self.wait_for([P_PRIV, P_USER], 45)
                transcript.append(text)
        return "\n".join(transcript), errors


def attach(a):
    hwnd, title = pick_window(a.window)
    logfile = a.log or DEFAULT_LOG
    # PuTTY creates the log lazily, on the first byte it displays. A missing
    # file on a fresh session is normal, so warn rather than refuse.
    if not os.path.exists(logfile):
        print("note: session log does not exist yet (%s).\n"
              "      PuTTY creates it on first output; sending a newline should"
              " do it." % logfile, file=sys.stderr)
    return Ghost(hwnd, title, logfile, a.delay, a.timeout, a.verbose)


def log_hint(logfile):
    return ("\nThe session log was never created:\n  " + logfile +
            "\nThat means this PuTTY is not logging to it. Either it was "
            "opened\nwithout the saved session, or logging is off. Fix with:\n"
            "  python ghost.py --port COMx launch")


# --- CLI -------------------------------------------------------------------
def cmd_windows(a):
    wins = find_putty_windows(a.window)
    if not wins:
        print("No PuTTY windows open.")
        return 1
    for h, t in wins:
        print("hwnd %-10d %s" % (h, t))
    return 0


def cmd_setup(a):
    port = a.port if a.port and a.port.lower() != "auto" else autodetect()
    if not port:
        sys.exit("No console port detected, and none given.\n"
                 "Plug the cable in, or pass --port COMx explicitly.")
    logfile = a.log or DEFAULT_LOG
    key = setup_session(port, a.baud, logfile, a.name)
    print("Saved PuTTY session %r configured:" % a.name)
    print("  registry : HKCU\\" + key)
    print("  serial   : %s @ %d 8N1, no flow control" % (port, a.baud))
    print("  logging  : all session output -> %s (flush on)" % logfile)
    print("\nOpen it yourself with:  putty -load \"%s\"" % a.name)
    return 0


def cmd_launch(a):
    port = a.port if a.port and a.port.lower() != "auto" else autodetect()
    if not port:
        rows = find_ports()
        msg = ["No USB-serial console port detected."]
        for r in rows:
            tag = "   (Intel AMT SOL - not a console cable)" if r["sol"] else ""
            msg.append("  " + r["device"] + "  " + r["desc"] + tag)
        msg.append("Plug the cable in, or pass --port COMx explicitly.")
        sys.exit("\n".join(msg))
    logfile = a.log or DEFAULT_LOG
    launch(port, a.baud, logfile, a.name)
    print("Launched PuTTY on %s @ %d, logging to:\n  %s" % (port, a.baud, logfile))
    time.sleep(1.5)
    for h, t in find_putty_windows():
        print("  window: %s (hwnd %d)" % (t, h))
    return 0


def cmd_probe(a):
    g = attach(a)
    print("window   : %s" % g.title, file=sys.stderr)
    state = g.probe()
    print("state    : " + state)
    print("hostname : " + (g.hostname or "(unknown)"))
    if state == "no-response":
        if not os.path.exists(g.logfile):
            print(log_hint(g.logfile))
        else:
            print("\nThe log exists but nothing came back. Check the cable is in"
                  " CONSOLE,\nthe device is powered, and the baud rate is right"
                  " (try --baud 115200).")
        return 1
    return 0


def cmd_type(a):
    g = attach(a)
    text = a.text.replace("\\r", "\r").replace("\\n", "\r").replace("\\t", "\t")
    g.send_raw(text)
    if a.enter:
        g.send_raw("\r")
    time.sleep(0.4)
    out = g.read_new()
    if out:
        print(clean(out))
    return 0


def cmd_run(a):
    g = attach(a)
    rc = 0
    for cmd in a.command:
        out, ok = g.command(cmd)
        print("===== " + cmd + " =====")
        print(out)
        print()
        if not ok:
            rc = 1
    return rc


def cmd_config(a):
    if a.file:
        with open(a.file, encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = a.line
    if not lines:
        sys.exit("Nothing to apply: pass --line or --file.")
    g = attach(a)
    transcript, errors = g.config(lines, save=a.save)
    print(transcript)
    if errors:
        print("\n!!! IOS rejected these lines:", file=sys.stderr)
        for e in errors:
            print("  " + e, file=sys.stderr)
        return 1
    print("\nApplied cleanly." +
          ("  Saved to startup-config." if a.save else
           "  (running-config only; --save to persist)"))
    return 0


def cmd_watch(a):
    """Passive tail of the session log -- see what the router is saying."""
    g = attach(a)
    print("Watching %s   (Ctrl+C to stop)" % g.logfile, file=sys.stderr)
    try:
        while True:
            chunk = g.read_new()
            if chunk:
                sys.stdout.write(clean(chunk))
                sys.stdout.flush()
            else:
                time.sleep(0.1)
    except KeyboardInterrupt:
        return 0


def main():
    def shared(p, suppress):
        """Options accepted either before or after the subcommand.

        The copy attached to each subparser defaults to SUPPRESS, so leaving an
        option out after the subcommand does not clobber a value given before
        it.
        """
        def dflt(v):
            return argparse.SUPPRESS if suppress else v
        p.add_argument("--window", default=dflt(None),
                       help="substring of the PuTTY window title")
        p.add_argument("--log", default=dflt(None), help="PuTTY session log path")
        p.add_argument("--port", default=dflt("auto"))
        p.add_argument("--baud", type=int, default=dflt(9600))
        p.add_argument("--name", default=dflt(SESSION_NAME),
                       help="PuTTY saved session name")
        p.add_argument("--delay", type=float, default=dflt(0.025),
                       help="seconds between keystrokes (default 0.025)")
        p.add_argument("--timeout", type=float, default=dflt(20.0))
        p.add_argument("-v", "--verbose", action="store_true",
                       default=dflt(False))

    ap = argparse.ArgumentParser(
        description="Ghost-type into a PuTTY serial session to a Cisco device.")
    shared(ap, suppress=False)
    common = argparse.ArgumentParser(add_help=False)
    shared(common, suppress=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser

    def sub_add(name, **kw):
        return _add(name, parents=[common], **kw)
    sub.add_parser = sub_add

    sub.add_parser("windows", help="list open PuTTY windows").set_defaults(fn=cmd_windows)
    sub.add_parser("setup", help="create the saved PuTTY session").set_defaults(fn=cmd_setup)
    sub.add_parser("launch", help="setup + open PuTTY on the console port").set_defaults(fn=cmd_launch)
    sub.add_parser("probe", help="press Enter, report the prompt").set_defaults(fn=cmd_probe)
    sub.add_parser("watch", help="tail the session log").set_defaults(fn=cmd_watch)

    p = sub.add_parser("type", help="type literal text into the window")
    p.add_argument("text")
    p.add_argument("--enter", action="store_true", help="append Enter")
    p.set_defaults(fn=cmd_type)

    p = sub.add_parser("run", help="run exec commands, capture output")
    p.add_argument("command", nargs="+")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("config", help="apply config lines")
    p.add_argument("--line", action="append", default=[])
    p.add_argument("--file")
    p.add_argument("--save", action="store_true")
    p.set_defaults(fn=cmd_config)

    a = ap.parse_args()
    try:
        sys.exit(a.fn(a))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
