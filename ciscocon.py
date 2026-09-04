#!/usr/bin/env python3
"""
ciscocon.py -- drive a Cisco device over a serial console (USB-to-RJ45 cable).

Talks to the COM port directly instead of automating the PuTTY GUI, so output
is captured as text rather than scraped off a screen.

NOTE: a serial port is exclusive. Close PuTTY before using this.
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial missing.  Install with:  pip install pyserial")


# --- prompt patterns -------------------------------------------------------
# Anchored to the tail of the buffer: we only match a prompt the device is
# actually sitting at, not one that scrolled past inside command output.
P_USER     = re.compile(r"(?:^|\n)([\w.\-]+)>\s*$")
P_PRIV     = re.compile(r"(?:^|\n)([\w.\-]+)#\s*$")
P_CONFIG   = re.compile(r"(?:^|\n)([\w.\-]+)\(config[^)]*\)#\s*$")
P_ROMMON   = re.compile(r"(?:^|\n)(rommon\s*\d+\s*>)\s*$", re.I)
P_MORE     = re.compile(r"--\s*More\s*--")
P_USERNAME = re.compile(r"Username:\s*$", re.I)
P_PASSWORD = re.compile(r"Password:\s*$", re.I)
P_INITIAL  = re.compile(r"initial configuration dialog\?\s*\[yes/no\]:\s*$", re.I)
P_RETURN   = re.compile(r"Press RETURN to get started", re.I)
P_CONFIRM  = re.compile(r"\[confirm\]\s*$", re.I)
P_YESNO    = re.compile(r"\[yes/no\]:\s*$", re.I)
P_SAVEQ    = re.compile(r"\[startup-config\]\?\s*$|Destination filename.*\?\s*$", re.I)

# IOS complaints worth surfacing to the caller.
P_IOSERR = re.compile(
    r"^%\s*(Invalid input|Incomplete command|Ambiguous command|"
    r"Unknown command|Bad IP address|Error)", re.M | re.I)

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def clean(text):
    """Strip ANSI, resolve backspaces, normalise line endings."""
    text = ANSI.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for ch in text:
        if ch == "\x08":
            if out:
                out.pop()
        elif ch != "\x00":
            out.append(ch)
    return "".join(out)


def find_ports():
    """All serial ports, with the Intel AMT SOL virtual port flagged."""
    rows = []
    for p in list_ports.comports():
        desc = p.description or ""
        hwid = p.hwid or ""
        is_sol = bool(re.search(
            r"serial.over.lan|\bSOL\b|Active Management", desc, re.I))
        is_usb = bool(re.search(
            r"USB|Prolific|FTDI|CP210|CH340|CH910|Silicon Labs|Cisco|Console",
            desc + " " + hwid, re.I))
        rows.append({"device": p.device, "desc": desc, "hwid": hwid,
                     "sol": is_sol, "usb": is_usb})
    return rows


def autodetect():
    rows = find_ports()
    usb = [r for r in rows if r["usb"] and not r["sol"]]
    if usb:
        return usb[0]["device"]
    other = [r for r in rows if not r["sol"]]
    if len(other) == 1:
        return other[0]["device"]
    return None


class Console:
    def __init__(self, port, baud=9600, timeout=15, logfile=None, verbose=False):
        self.port_name = port
        self.baud = baud
        self.timeout = timeout
        self.verbose = verbose
        self.ser = None
        self.hostname = None
        self.log = open(logfile, "a", encoding="utf-8") if logfile else None
        if self.log:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.log.write(
                "\n===== session " + stamp + " " + port +
                " @ " + str(baud) + " =====\n")

    # -- plumbing -----------------------------------------------------------
    def open(self):
        # Cisco console defaults: 9600 8N1, no flow control.
        self.ser = serial.Serial(
            port=self.port_name, baudrate=self.baud,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            xonxoff=False, rtscts=False, dsrdtr=False,
            timeout=0.2, write_timeout=5,
        )
        # Some USB adapters keep the device muted until these are asserted.
        try:
            self.ser.dtr = True
            self.ser.rts = True
        except Exception:
            pass
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        return self

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
        if self.log:
            self.log.close()

    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close()

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        self.ser.write(data)
        self.ser.flush()

    def send_line(self, line=""):
        self.write(line + "\r")

    def read_until(self, patterns, timeout=None, send_on_more=True):
        """Read until one of `patterns` matches the tail.

        Returns (cleaned_text, match_or_None, pattern_or_None).
        """
        timeout = self.timeout if timeout is None else timeout
        deadline = time.time() + timeout
        chunks = []
        while time.time() < deadline:
            n = self.ser.in_waiting
            data = self.ser.read(n if n else 1)
            if data:
                piece = data.decode("utf-8", "replace")
                chunks.append(piece)
                if self.log:
                    self.log.write(piece)
                if self.verbose:
                    sys.stderr.write(piece)
                tail = clean("".join(chunks))
                # Pager: feed it a space and keep reading. Not a resting prompt.
                if send_on_more and P_MORE.search(tail[-80:]):
                    self.write(" ")
                    chunks.append("\n")
                    continue
                for pat in patterns:
                    m = pat.search(tail)
                    if m:
                        return tail, m, pat
            else:
                time.sleep(0.02)
        return clean("".join(chunks)), None, None

    def at_prompt(self, timeout=None):
        """Any resting prompt."""
        return self.read_until([P_CONFIG, P_PRIV, P_USER, P_ROMMON], timeout)

    # -- session ------------------------------------------------------------
    def wake(self, enable_password=None, username=None, password=None,
             answer_initial_dialog=True):
        """Nudge the console and get to a known prompt. Returns a state string."""
        for _attempt in range(4):
            self.send_line("")
            text, m, pat = self.read_until(
                [P_CONFIG, P_PRIV, P_USER, P_ROMMON, P_USERNAME,
                 P_PASSWORD, P_INITIAL, P_RETURN],
                timeout=6)

            if pat is P_INITIAL:
                if not answer_initial_dialog:
                    return "initial-dialog"
                self.send_line("no")          # skip the setup wizard
                time.sleep(1)
                continue
            if pat is P_RETURN:
                self.send_line("")
                time.sleep(0.5)
                continue
            if pat is P_USERNAME:
                if username is None:
                    return "needs-username"
                self.send_line(username)
                self.read_until([P_PASSWORD], timeout=6)
                self.send_line(password or "")
                time.sleep(0.5)
                continue
            if pat is P_PASSWORD:
                if password is None and enable_password is None:
                    return "needs-password"
                self.send_line(
                    password if password is not None else enable_password)
                time.sleep(0.5)
                continue
            if pat is P_CONFIG:
                self.hostname = m.group(1)
                return "config"
            if pat is P_PRIV:
                self.hostname = m.group(1)
                return "privileged"
            if pat is P_USER:
                self.hostname = m.group(1)
                return "user"
            if pat is P_ROMMON:
                return "rommon"
        return "no-response"

    def enable(self, enable_password=None):
        """Escalate user EXEC -> privileged EXEC."""
        self.send_line("enable")
        text, m, pat = self.read_until([P_PRIV, P_PASSWORD, P_USER], timeout=8)
        if pat is P_PASSWORD:
            if enable_password is None:
                return False, "enable password required but none supplied"
            self.send_line(enable_password)
            text, m, pat = self.read_until(
                [P_PRIV, P_PASSWORD, P_USER], timeout=8)
            if pat is P_PASSWORD:
                return False, "enable password rejected"
        if pat is P_PRIV:
            self.hostname = m.group(1)
            return True, "privileged"
        return False, "could not reach privileged mode"

    def no_pager(self):
        self.send_line("terminal length 0")
        self.at_prompt(timeout=6)

    def command(self, cmd, timeout=None):
        """Run one command; return output with echo and trailing prompt removed."""
        self.ser.reset_input_buffer()
        self.send_line(cmd)
        text, m, pat = self.read_until([P_CONFIG, P_PRIV, P_USER], timeout)
        lines = text.split("\n")
        # Drop the echoed command line.
        if lines and cmd.strip() and cmd.strip() in lines[0]:
            lines = lines[1:]
        # Drop the trailing prompt.
        if lines and re.match(r"^[\w.\-]+(\([^)]*\))?[>#]\s*$", lines[-1]):
            lines = lines[:-1]
        return "\n".join(lines).strip("\n"), (pat is not None)

    def config(self, lines, save=False):
        """Apply configuration lines. Returns (transcript, errors)."""
        transcript, errors = [], []
        self.send_line("configure terminal")
        text, m, pat = self.read_until([P_CONFIG, P_PRIV, P_USER], timeout=10)
        transcript.append(text)
        if pat is not P_CONFIG:
            return "\n".join(transcript), ["could not enter configuration mode"]

        for line in lines:
            line = line.rstrip()
            if not line or line.lstrip().startswith("!"):
                continue
            out, _ok = self.command(line, timeout=self.timeout)
            transcript.append(line + "\n" + out if out else line)
            if P_IOSERR.search(out):
                first = out.strip().splitlines()[0]
                errors.append(line + "  ->  " + first)

        self.send_line("end")
        text, _, _ = self.read_until([P_PRIV, P_USER], timeout=10)
        transcript.append(text)

        if save:
            self.send_line("write memory")
            text, m, pat = self.read_until(
                [P_PRIV, P_USER, P_CONFIRM, P_SAVEQ], timeout=45)
            transcript.append(text)
            if pat in (P_CONFIRM, P_SAVEQ):
                self.send_line("")
                text, _, _ = self.read_until([P_PRIV, P_USER], timeout=45)
                transcript.append(text)
        return "\n".join(transcript), errors


# --- CLI -------------------------------------------------------------------
def resolve_port(arg):
    if arg and arg.lower() != "auto":
        return arg
    p = autodetect()
    if not p:
        rows = find_ports()
        msg = ["No USB-serial console port found."]
        if rows:
            msg.append("Ports present:")
            for r in rows:
                tag = "   (Intel AMT SOL - not a console cable)" if r["sol"] else ""
                msg.append("  " + r["device"] + "  " + r["desc"] + tag)
        else:
            msg.append("No serial ports at all - is the cable plugged in?")
        sys.exit("\n".join(msg))
    return p


def cmd_ports(a):
    rows = find_ports()
    if not rows:
        print("No serial ports found.")
        return 1
    for r in rows:
        tags = []
        if r["sol"]:
            tags.append("Intel AMT SOL - NOT a console cable")
        if r["usb"]:
            tags.append("USB-serial - likely your console cable")
        print("{:<8} {}".format(r["device"], r["desc"]))
        print("{:<8} {}".format("", r["hwid"]))
        if tags:
            print("{:<8} >> {}".format("", "; ".join(tags)))
    guess = autodetect()
    print("\nAutodetect picks: " + (guess or "nothing usable"))
    return 0


def cmd_probe(a):
    port = resolve_port(a.port)
    print("Opening {} @ {} 8N1...".format(port, a.baud), file=sys.stderr)
    with Console(port, a.baud, a.timeout, a.log, a.verbose) as c:
        state = c.wake(a.enable_password, a.username, a.password)
        print("state    : " + state)
        print("hostname : " + (c.hostname or "(unknown)"))
        if state == "no-response":
            print("\nNo reply. Check: cable in CONSOLE (not AUX/ETH), device\n"
                  "powered, PuTTY closed, and baud (try --baud 115200).")
            return 1
        if state == "user":
            ok, msg = c.enable(a.enable_password)
            print("enable   : " + msg)
            if not ok:
                return 1
        if state in ("user", "privileged", "config"):
            c.no_pager()
            out, _ = c.command(
                "show version | include (uptime|Version|IOS|Model|System image)")
            print("\n--- show version ---")
            print(out)
        return 0


def cmd_run(a):
    port = resolve_port(a.port)
    with Console(port, a.baud, a.timeout, a.log, a.verbose) as c:
        state = c.wake(a.enable_password, a.username, a.password)
        if state == "no-response":
            sys.exit("No response from console.")
        if state == "user" and a.enable_password is not None:
            c.enable(a.enable_password)
        c.no_pager()
        rc = 0
        for cmd in a.command:
            out, ok = c.command(cmd, a.timeout)
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

    port = resolve_port(a.port)
    with Console(port, a.baud, a.timeout, a.log, a.verbose) as c:
        state = c.wake(a.enable_password, a.username, a.password)
        if state == "no-response":
            sys.exit("No response from console.")
        if state == "user":
            ok, msg = c.enable(a.enable_password)
            if not ok:
                sys.exit(msg)
        c.no_pager()
        transcript, errors = c.config(lines, save=a.save)
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


def cmd_raw(a):
    port = resolve_port(a.port)
    with Console(port, a.baud, a.timeout, a.log, a.verbose) as c:
        c.write(a.text.replace("\\r", "\r").replace("\\n", "\n"))
        text, _, _ = c.read_until(
            [P_CONFIG, P_PRIV, P_USER, P_PASSWORD, P_USERNAME,
             P_CONFIRM, P_YESNO], a.timeout)
        print(text)
        return 0


def main():
    ap = argparse.ArgumentParser(
        description="Drive a Cisco device over a serial console.")
    ap.add_argument("--port", default="auto", help="COM port, or 'auto' (default)")
    ap.add_argument("--baud", type=int, default=9600, help="default 9600")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--enable-password", default=None)
    ap.add_argument("--username", default=None)
    ap.add_argument("--password", default=None, help="console/line password")
    ap.add_argument("--log", default=None, help="append raw session to this file")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="mirror serial traffic to stderr live")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ports", help="list serial ports").set_defaults(fn=cmd_ports)
    sub.add_parser(
        "probe", help="wake console, report mode + version").set_defaults(fn=cmd_probe)

    p = sub.add_parser("run", help="run exec commands")
    p.add_argument("command", nargs="+")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("config", help="apply config lines")
    p.add_argument("--line", action="append", default=[])
    p.add_argument("--file")
    p.add_argument("--save", action="store_true", help="write memory afterwards")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("raw", help="send raw text, print what comes back")
    p.add_argument("text")
    p.set_defaults(fn=cmd_raw)

    a = ap.parse_args()
    # Prefer the environment: anything on argv is visible in the process list.
    a.enable_password = a.enable_password or os.environ.get("CISCO_ENABLE_PASSWORD")
    a.password = a.password or os.environ.get("CISCO_PASSWORD")
    a.username = a.username or os.environ.get("CISCO_USERNAME")
    try:
        sys.exit(a.fn(a))
    except serial.SerialException as e:
        sys.exit("Serial error: {}\n"
                 "If this says 'Access is denied', PuTTY still has the "
                 "port open.".format(e))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
