#!/usr/bin/env python3
"""
Offline test: fake Cisco IOS device on a fake serial port.

Exercises read_until / clean / command echo-stripping / pager handling /
enable escalation / config error detection -- without hardware.
"""

import sys
import ciscocon
from ciscocon import Console


class FakeIOS:
    """Minimal IOS state machine speaking over a fake serial port."""

    def __init__(self, hostname="R1", enable_pw="cisco", pager=True):
        self.hostname = hostname
        self.enable_pw = enable_pw
        self.pager = pager
        self.mode = "user"           # user | priv | config
        self.awaiting_enable_pw = False
        self.out = bytearray()
        self.line = ""
        self.is_open = True
        self.dtr = self.rts = False
        self.applied = []
        self.saved = False
        self._more_pending = None

    # --- serial API surface ---
    @property
    def in_waiting(self):
        return len(self.out)

    def read(self, n=1):
        if not self.out:
            return b""
        n = min(n, len(self.out))
        data = bytes(self.out[:n])
        del self.out[:n]
        return data

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.out.clear()

    def close(self):
        self.is_open = False

    def write(self, data):
        for ch in data.decode("utf-8", "replace"):
            if self._more_pending is not None:
                # Pager is waiting for a keypress.
                rest = self._more_pending
                self._more_pending = None
                self.emit("\x08" * 10 + " " * 10 + "\x08" * 10)
                self.emit(rest)
                self.prompt()
                continue
            if ch == "\r":
                self.handle(self.line)
                self.line = ""
            elif ch == "\n":
                pass
            else:
                self.line += ch
                self.emit(ch)          # echo, like a real console
        return len(data)

    # --- device behaviour ---
    def emit(self, s):
        self.out.extend(s.encode())

    def prompt(self):
        if self.mode == "user":
            self.emit("\r\n" + self.hostname + ">")
        elif self.mode == "priv":
            self.emit("\r\n" + self.hostname + "#")
        else:
            self.emit("\r\n" + self.hostname + "(config)#")

    def handle(self, cmd):
        cmd = cmd.strip()

        if self.awaiting_enable_pw:
            self.awaiting_enable_pw = False
            if cmd == self.enable_pw:
                self.mode = "priv"
            else:
                self.emit("\r\n% Access denied")
            self.prompt()
            return

        if cmd == "":
            self.prompt()
            return

        if cmd == "enable" and self.mode == "user":
            self.awaiting_enable_pw = True
            self.emit("\r\nPassword:")
            return

        if cmd == "terminal length 0":
            self.pager = False
            self.prompt()
            return

        if self.mode == "config":
            if cmd == "end" or cmd == "exit":
                self.mode = "priv"
                self.prompt()
                return
            if cmd.startswith("bogus"):
                self.emit("\r\n" + " " * 17 + "^")
                self.emit("\r\n% Invalid input detected at '^' marker.\r\n")
                self.prompt()
                return
            self.applied.append(cmd)
            self.prompt()
            return

        if cmd == "configure terminal":
            if self.mode != "priv":
                self.emit("\r\n% Invalid input detected at '^' marker.")
                self.prompt()
                return
            self.mode = "config"
            self.emit("\r\nEnter configuration commands, one per line."
                      "  End with CNTL/Z.")
            self.prompt()
            return

        if cmd == "write memory":
            self.saved = True
            self.emit("\r\nBuilding configuration...\r\n[OK]")
            self.prompt()
            return

        if cmd.startswith("show version"):
            body = ("\r\nCisco IOS Software, C2900 Software, Version 15.7(3)M"
                    "\r\nR1 uptime is 3 days, 4 hours, 12 minutes"
                    "\r\nSystem image file is \"flash0:c2900-universalk9.bin\"")
            self.paged(body)
            return

        if cmd.startswith("show run"):
            body = "\r\n" + "\r\n".join(
                "! config line %d" % i for i in range(1, 60))
            self.paged(body)
            return

        self.emit("\r\n% Unknown command")
        self.prompt()

    def paged(self, body):
        """Emit body, inserting a --More-- break if the pager is on."""
        if self.pager and len(body) > 120:
            head, rest = body[:120], body[120:]
            self.emit(head)
            self.emit("\r\n --More-- ")
            self._more_pending = rest
        else:
            self.emit(body)
            self.prompt()


def make_console(dev, **kw):
    """Build a Console wired to a FakeIOS instead of a real port."""
    c = Console("FAKE", **kw)
    c.ser = dev
    return c


FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  PASS  " + name)
    else:
        print("  FAIL  " + name + ("   " + detail if detail else ""))
        FAILS.append(name)


def test_wake_user():
    print("\n[wake at user EXEC]")
    dev = FakeIOS()
    c = make_console(dev)
    state = c.wake()
    check("state is 'user'", state == "user", "got " + repr(state))
    check("hostname parsed", c.hostname == "R1", "got " + repr(c.hostname))


def test_enable():
    print("\n[enable escalation]")
    dev = FakeIOS(enable_pw="cisco")
    c = make_console(dev)
    c.wake()
    ok, msg = c.enable("cisco")
    check("enable succeeds", ok, msg)
    check("device now privileged", dev.mode == "priv", dev.mode)

    dev2 = FakeIOS(enable_pw="cisco")
    c2 = make_console(dev2)
    c2.wake()
    ok2, msg2 = c2.enable("wrongpw")
    check("wrong password rejected", not ok2, msg2)


def test_command_output():
    print("\n[command output stripping]")
    dev = FakeIOS()
    c = make_console(dev)
    c.wake()
    c.enable("cisco")
    c.no_pager()
    out, ok = c.command("show version")
    check("completed at prompt", ok)
    check("echo line removed", not out.startswith("show version"),
          repr(out[:40]))
    check("version text present", "15.7(3)M" in out, repr(out[:80]))
    check("no trailing prompt", not out.rstrip().endswith("#"),
          repr(out[-30:]))


def test_pager():
    print("\n[--More-- pager handling]")
    dev = FakeIOS(pager=True)
    c = make_console(dev)
    c.wake()
    c.enable("cisco")
    # Deliberately do NOT call no_pager(), to force a --More-- break.
    out, ok = c.command("show running-config", timeout=8)
    check("completed at prompt", ok)
    check("no --More-- left in output",
          "More" not in out, repr(out[:120]))
    check("content past the break captured",
          "config line 59" in out, repr(out[-80:]))


def test_config_ok():
    print("\n[config apply, clean]")
    dev = FakeIOS()
    c = make_console(dev)
    c.wake()
    c.enable("cisco")
    c.no_pager()
    lines = ["hostname R1", "! a comment", "", "interface Loopback0",
             " ip address 10.0.0.1 255.255.255.255"]
    transcript, errors = c.config(lines, save=True)
    check("no errors reported", errors == [], str(errors))
    check("comments/blanks skipped", len(dev.applied) == 3,
          str(dev.applied))
    check("lines reached device",
          "interface Loopback0" in dev.applied, str(dev.applied))
    check("write memory ran", dev.saved)
    check("back at priv EXEC", dev.mode == "priv", dev.mode)


def test_config_error():
    print("\n[config apply, IOS rejects a line]")
    dev = FakeIOS()
    c = make_console(dev)
    c.wake()
    c.enable("cisco")
    c.no_pager()
    transcript, errors = c.config(["hostname R2", "bogus command here"])
    check("error detected", len(errors) == 1, str(errors))
    check("error names the bad line",
          errors and "bogus command here" in errors[0], str(errors))
    check("good line still applied",
          "hostname R2" in dev.applied, str(dev.applied))


def test_clean():
    print("\n[clean() helper]")
    check("backspaces resolved", ciscocon.clean("abc\x08\x08X") == "aX")
    check("ANSI stripped",
          ciscocon.clean("\x1b[0mhi\x1b[31m") == "hi")
    check("CRLF normalised", ciscocon.clean("a\r\nb") == "a\nb")


def main():
    for fn in (test_wake_user, test_enable, test_command_output, test_pager,
               test_config_ok, test_config_error, test_clean):
        fn()
    print("\n" + "=" * 50)
    if FAILS:
        print("FAILED: " + ", ".join(FAILS))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
