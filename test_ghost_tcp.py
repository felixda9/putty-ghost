#!/usr/bin/env python3
"""
Full end-to-end test of the ghost typer against REAL PuTTY, no router needed.

The simulated IOS device from test_mock.py is served over a loopback TCP
socket. PuTTY connects to it in raw mode with session logging on, and we drive
it exactly as we would drive a serial console:

  ghost.py --PostMessage(WM_CHAR)--> PuTTY --TCP--> FakeIOS
  FakeIOS --TCP--> PuTTY --> terminal --> session log --> ghost.py

Everything except the serial transport itself is the real code path.
"""

import ctypes
import ctypes.wintypes as wt
import os
import socket
import sys
import threading
import time
import winreg

import ghost
from ghost import find_putty_windows, Ghost, clean
from test_mock import FakeIOS

TEST_SESSION = "ghost-selftest-tcp"
HERE = os.path.dirname(os.path.abspath(__file__))
TEST_LOG = os.path.join(HERE, "logs", "selftest-tcp.log")
PUTTY = ghost.find_putty()
SESSKEY = r"SOFTWARE\SimonTatham\PuTTY\Sessions\%s" % TEST_SESSION

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  PASS  " + name)
    else:
        print("  FAIL  " + name + ("   " + detail if detail else ""))
        FAILS.append(name)


class IOSServer(threading.Thread):
    """Serve one FakeIOS over a loopback TCP connection."""

    daemon = True

    def __init__(self):
        super().__init__()
        self.dev = FakeIOS(hostname="R1", enable_pw="cisco")
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(1)
        self.stop = False

    def run(self):
        self.sock.settimeout(30)
        try:
            conn, _ = self.sock.accept()
        except socket.timeout:
            return
        conn.settimeout(0.05)
        with conn:
            while not self.stop:
                try:
                    data = conn.recv(4096)
                    if not data:
                        break
                    self.dev.write(data)
                except socket.timeout:
                    pass
                except OSError:
                    break
                if self.dev.out:
                    try:
                        conn.sendall(bytes(self.dev.out))
                    except OSError:
                        break
                    self.dev.out.clear()


def make_session(port):
    os.makedirs(os.path.dirname(TEST_LOG), exist_ok=True)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, SESSKEY) as k:
        def s(n, v):
            winreg.SetValueEx(k, n, 0, winreg.REG_SZ, v)

        def d(n, v):
            winreg.SetValueEx(k, n, 0, winreg.REG_DWORD, v)

        s("Protocol", "raw")
        s("HostName", "127.0.0.1")
        d("PortNumber", port)
        s("LogFileName", TEST_LOG)
        d("LogType", 2)        # all session output
        d("LogFlush", 1)
        d("LogFileClash", 1)   # append, never prompt
        d("CloseOnExit", 0)
        # The device echoes, exactly like a real console -- so no local echo
        # or line editing, or we would see doubled characters.
        d("LocalEcho", 1)      # FORCE_OFF
        d("LocalEdit", 1)      # FORCE_OFF


def cleanup(hwnd=None):
    if hwnd:
        try:
            ghost.user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
            time.sleep(0.6)
        except Exception:
            pass
        # PuTTY asks for confirmation while a session is live, so the window
        # can outlive WM_CLOSE. Kill the process that owns it.
        if ghost.user32.IsWindow(hwnd):
            pid = wt.DWORD()
            ghost.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            os.system("taskkill /PID %d /F >nul 2>&1" % pid.value)
            time.sleep(0.3)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, SESSKEY)
    except OSError:
        pass
    try:
        os.remove(TEST_LOG)
    except OSError:
        pass


def main():
    if not os.path.exists(PUTTY):
        sys.exit("putty.exe not found")

    srv = IOSServer()
    srv.start()
    print("[setup] fake IOS listening on 127.0.0.1:%d" % srv.port)
    make_session(srv.port)
    if os.path.exists(TEST_LOG):
        os.remove(TEST_LOG)

    before = {h for h, _ in find_putty_windows()}
    os.spawnv(os.P_NOWAIT, PUTTY, [PUTTY, "-load", TEST_SESSION])

    hwnd = title = None
    for _ in range(60):
        time.sleep(0.2)
        new = [(h, t) for h, t in find_putty_windows() if h not in before]
        if new:
            hwnd, title = new[0]
            break
    check("PuTTY window opened", hwnd is not None)
    if hwnd is None:
        cleanup()
        return 1
    print("        window: %r (hwnd %d)" % (title, hwnd))
    time.sleep(1.0)

    g = Ghost(hwnd, title, TEST_LOG, delay=0.005, timeout=12)

    # -- probe: press Enter, expect the user-EXEC prompt --
    print("[test] probe")
    state = g.probe(timeout=10)
    check("log file created by PuTTY", os.path.exists(TEST_LOG), TEST_LOG)
    check("probe reports user EXEC", state == "user", "got " + repr(state))
    check("hostname parsed from prompt", g.hostname == "R1",
          "got " + repr(g.hostname))

    # -- enable escalation, typed through the window --
    print("[test] enable")
    g.send_line("enable")
    text, m, pat = g.wait_for([ghost.P_PASSWORD, ghost.P_PRIV], 10)
    check("enable prompted for password", pat is ghost.P_PASSWORD,
          repr(text[-60:]))
    g.send_line("cisco")
    text, m, pat = g.wait_for([ghost.P_PRIV, ghost.P_USER], 10)
    check("reached privileged EXEC", pat is ghost.P_PRIV, repr(text[-60:]))
    check("device agrees it is privileged", srv.dev.mode == "priv",
          srv.dev.mode)

    # -- run a command and capture clean output --
    print("[test] run 'show version'")
    g.send_line("terminal length 0")
    g.wait_for([ghost.P_PRIV], 8)
    out, ok = g.command("show version", timeout=12)
    check("command completed at prompt", ok)
    check("output captured", "15.7(3)M" in out, repr(out[:100]))
    check("echoed command stripped", not out.startswith("show version"),
          repr(out[:40]))

    # -- pager handling through the real terminal --
    print("[test] --More-- pager")
    srv.dev.pager = True
    out, ok = g.command("show running-config", timeout=15)
    check("paged output completed", ok)
    check("no --More-- marker left in output", "More" not in out,
          repr(out[:100]))
    check("content past the pager break captured",
          "config line 59" in out, repr(out[-80:]))

    # -- config apply, with a deliberately bad line --
    print("[test] config apply with a bad line")
    transcript, errors = g.config(
        ["hostname R1", "! comment", "interface Loopback0",
         " ip address 10.0.0.1 255.255.255.255", "bogus command here"])
    check("bad line detected", len(errors) == 1, str(errors))
    check("error names the offending line",
          errors and "bogus command here" in errors[0], str(errors))
    check("good lines reached the device",
          "interface Loopback0" in srv.dev.applied, str(srv.dev.applied))
    check("comment skipped",
          not any(l.startswith("!") for l in srv.dev.applied),
          str(srv.dev.applied))
    check("returned to privileged EXEC", srv.dev.mode == "priv", srv.dev.mode)

    print("[cleanup] closing PuTTY, removing test session")
    srv.stop = True
    cleanup(hwnd)

    print("\n" + "=" * 58)
    if FAILS:
        print("FAILED: " + ", ".join(FAILS))
        return 1
    print("Ghost typer verified end-to-end through real PuTTY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
