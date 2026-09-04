# putty-ghost

Type commands into your own PuTTY serial session from a script, and read the
device's replies back, so you can automate Cisco console work while watching
every keystroke land on screen.

## The problem

A COM port is exclusive. Normal console automation opens the port itself, which
means PuTTY has to be closed, and you lose sight of what is being sent to your
router.

putty-ghost goes through PuTTY instead of around it. It posts keystrokes to the
window you already have open, and reads the replies out of PuTTY's session log.

```
  ghost.py --PostMessage(WM_CHAR)--> PuTTY window --serial--> router
                                          ^                      |
        router replies --> PuTTY terminal +--> session log ------+--> ghost.py
```

Commands appear character by character in your terminal, exactly as if you had
typed them. You can read along, and you can take over at any time by typing.

## Intended use

Open PuTTY on the console port and keep the window in front of you. Then drive
it either way:

**With Claude Code.** Describe the change you want in plain language. It works
out the commands and sends them through this tool while you watch each one
appear in the terminal. This is what the tool is built for, and it is the reason
output is captured as text rather than scraped off the screen.

**As a plain CLI.** The subcommands below stand on their own, with no AI
involved.

Either way you keep control. Nothing reaches the device that you cannot see, and
you can take over at any point by typing in the window yourself.

Suited to lab work, teaching, and any change where you want to watch each line
land before the next one goes in.

## Requirements

Windows, PuTTY 0.78+, Python 3.8+.

```
pip install -r requirements.txt
```

## Usage

```
python ghost.py launch                    open PuTTY, serial and logging preset
python ghost.py probe                     press Enter, report the prompt found
python ghost.py run "show ip int brief"
python ghost.py config --file r1.cfg --save
```

`launch` creates a PuTTY saved session called `cisco-console` (serial, 9600 8N1,
no flow control, all session output logged with flush on). You can open it
yourself with `putty -load cisco-console`.

If PuTTY is already open, skip `launch`. Every other command attaches to the
existing window. With more than one open, pick with `--window "COM4"`.

## Commands

| Command | Does |
|---|---|
| `windows` | List open PuTTY windows |
| `setup` | Create the saved PuTTY session |
| `launch` | Setup, then open PuTTY on the console port |
| `probe` | Press Enter, report prompt, mode and hostname |
| `run CMD...` | Run exec commands, print cleaned output |
| `config` | Apply config from `--line` or `--file` |
| `type TEXT` | Type literal text, `--enter` to append Enter |
| `watch` | Tail the session log |

Flags: `--window`, `--delay` (seconds per keystroke, default 0.025), `--port`,
`--baud`, `--log`, `-v`.

The typing delay keeps output readable and stays under what a 9600 baud console
can absorb. Consoles drop characters if you paste at them.

## Handles

* `--More--` pagination
* `Press RETURN to get started` and the initial configuration dialog
* `Username:` / `Password:` prompts and `enable` escalation
* Command echo and trailing prompts stripped from captured output
* IOS `%` errors: a rejected config line is reported by name and the tool exits
  non-zero instead of scrolling past

Config changes apply to the running config. Pass `--save` to write memory.

## Direct port mode

`ciscocon.py` is the same engine talking to the COM port directly, for when you
do not need to watch and PuTTY is closed.

```
python ciscocon.py ports
python ciscocon.py run "show version"
python ciscocon.py config --file r1.cfg --save
```

Pass credentials in `CISCO_ENABLE_PASSWORD`, `CISCO_PASSWORD`, `CISCO_USERNAME`
rather than on the command line, where they are visible in the process list.

## Tests

Both run with no router and no cable attached.

```
python test_mock.py        engine against a simulated IOS device
python test_ghost_tcp.py   full loop through real PuTTY over a TCP socket
```

`test_ghost_tcp.py` serves a simulated IOS device on a loopback socket, points a
real PuTTY at it, and drives it through the ghost typer. Everything except the
serial transport is the production code path.

## Notes

* Windows only. Uses the Win32 message queue and the PuTTY registry.
* Injection uses `PostMessage`, so it does not steal focus. You can keep working
  in other windows while it types.
* Verified end to end against real PuTTY with a simulated device. Not yet run
  against physical hardware.
* IOS style prompts. NX-OS and IOS-XR are not specifically handled.
* On many machines COM3 is Intel AMT Serial-over-LAN, not a console cable. The
  port listing flags it and leaves it out of autodetection.

## License

MIT
