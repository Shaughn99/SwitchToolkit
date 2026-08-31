# SwitchToolkit

Bulk IOS-XE upgrade tool for Cisco Catalyst switches (C9200 / C9300 families),
with a Tkinter front end over a headless upgrade engine.

## Layout

| File | Role |
| --- | --- |
| `upgrade_engine.py` | All switch logic — connect, inventory, collect, copy, verify, reload. No UI. Reports progress through a callback. |
| `switch_upgrade_gui.py` | Tkinter front end. Network work runs on worker threads that push messages onto a queue; the UI thread drains it on a timer. |
| `check_captures.py` | Audits collected tech-support files for the sections a STIG scanner needs. |

The engine never prints and never calls `input()`, so it can be driven from the
GUI, a CLI, or a test harness without changes.

## Install and run

```bash
pip install -r requirements.txt
python switch_upgrade_gui.py
```

Build a single-file Windows executable:

```bash
pyinstaller --onefile --windowed switch_upgrade_gui.py
```

## Workflow

The tool splits the upgrade into two phases so nothing reboots unexpectedly.

**Inventory (read-only)** — reads hostname, model, serial, version, base MAC and
boot mode from each switch. Runs no config commands and changes nothing. Stacks
report one row per member. Results export to CSV.

Needs nothing but a username and password — no TFTP server, no image
configuration. Those are only validated when you start a prepare.

Scanning a whole subnet is expected. Each address gets a TCP probe on port 22
first (1.5s by default), so dead addresses are dropped in milliseconds instead
of waiting out an SSH handshake. The scan runs 20 addresses at a time by
default, Cancel takes effect immediately, and "Hide non-responding rows" clears
the empty addresses off the table once the scan finishes.

**File collection (read-only)** — pulls `show running-config` and/or
`show tech-support` from the selected switches into a folder you choose:

```
<output folder>/configs/hostname.txt
<output folder>/tech-support/hostname.txt
<output folder>/skipped_switches.txt       (only if something failed)
```

where `hostname` is whatever each switch reports as its own.

Tech-support captures are checked for completeness before they are written. A
scanner rejects a whole file over one absent section, and a capture that was cut
short still looks like valid output, so anything missing is collected on its own
and appended under the same `------------------ show x ------------------`
delimiter IOS uses. If a section still cannot be collected, the log says so
rather than leaving you to find out from the scanner.

Run it after an inventory and it reuses the hostnames already discovered and
skips the addresses that did not answer. It also works without one — each
switch is probed before it is dialled either way. Tech-support takes several
minutes per switch and produces large files, so it can be unticked. A hostname
that is already taken gets the IP appended rather than overwriting the earlier
file.

**Prepare (no reloads)** — runs only against switches the inventory found. Per
switch:

1. Identify the model PID and match it to a configured image by prefix.
2. Skip the switch if it is already on the target version.
3. Check flash for an existing copy of the image. If the MD5 matches, the TFTP
   transfer is skipped entirely. If it does not match, the file is deleted and
   re-transferred.
4. `install remove inactive` — run **before** the copy, since it deletes unused
   `.bin` files from flash.
5. Check free flash against image size plus buffer.
6. `copy tftp://<server>/<image> flash:`
7. `verify /md5 flash:<image>`
8. `boot system flash:<image>` (clearing any previous entry) and `write memory`.
9. Verify the BOOT variable against both `show boot` and
   `show run | include boot system`.

A switch only reaches "Ready to reload" once step 9 passes.

**Reload** — triggered manually, per switch or for the whole selection, either
one at a time or all at once. Each reload waits for the switch to answer ping,
reconnects, and confirms the running version matches the target.

A per-row Reload locks only that row. Reloading takes minutes, so an operator
working through a stack of switches can start the next one whenever they are
ready — the other rows stay live and several can be in flight at once. The
phase-wide buttons stay disabled until they all finish. The batch reload
buttons still own the window for the length of the run.

## Notes

- **Set the expected MD5.** With it blank, verification is skipped and the tool
  falls back to an exact file-size match. A silently truncated transfer is what
  causes a corrupt install.
- **A single BOOT entry is enforced.** A leftover second entry lets the switch
  fall through to an old image on reload, which caused a boot loop during
  testing, so it is treated as fatal.
- **Boot mode is informational.** The `copy` → `boot system` → `verify` →
  `reload` workflow behaves the same in BUNDLE or INSTALL mode. Mode only
  matters for the `install add/activate/commit` workflow, which this tool does
  not use.
- Copy progress is an elapsed-time estimate — IOS does not report a percentage
  over this channel. The elapsed seconds shown are real.
- `show tech-support` is read until the device prompt returns, not until the
  session goes quiet. The command stalls for long stretches while the switch
  gathers each section, and a quiet-period read ends the capture inside one of
  those pauses without any error.
- Credentials are entered in the UI at run time and are not persisted anywhere.
- The probe uses TCP/22 rather than ICMP, so a switch that drops ping still
  scans normally. Raise the probe timeout if switches are behind a slow link.
- A connection that fails in transit is retried once before the switch is given
  up on. Authentication failures are never retried — repeating a bad password
  risks locking the account.
- Prepare runs only against switches the inventory positively found, and says so
  if no inventory has been run. Scanning a `/24` to find ten switches leaves 240
  empty addresses in the list, and none of them are dialled. A switch that has
  since come back is picked up by re-running the inventory.
- File collection follows the same rule once an inventory has been run. Without
  one it still works on its own, relying on the per-switch probe.

## Accepted IP formats

```
192.168.1.45                 single address
192.168.1.1-100              last-octet range
192.168.1.1-192.168.1.50     full start-end range
192.168.1.0/24               CIDR, host addresses only
```

One per line or comma separated; ranges expand up to 1024 addresses. The
addresses above are only format examples — the TFTP server and switch list both
start empty for you to fill in.
