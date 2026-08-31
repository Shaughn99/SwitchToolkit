"""
upgrade_engine.py - IOS-XE upgrade logic, no user interface.

Every function here reports progress through a callback instead of
printing, and never blocks on input(). That keeps the switch logic
usable from a GUI, a CLI, or a test harness without changes.

The read-only work (inventory, config/tech-support collection) needs
nothing but credentials - no TFTP server and no image configuration.
Only the upgrade path uses those.

Command sequence per switch (unchanged from the validated manual process):
    install remove inactive
    copy tftp://<server>/<image> flash:
    verify /md5 flash:<image>
    configure terminal / no boot system / boot system flash:<image> / end / write memory
    show boot
    show run | include boot system
    reload
"""

import os
import re
import time
import socket
import platform
import subprocess
from dataclasses import dataclass, field
from netmiko import ConnectHandler


# ============================================================
# STATUS CONSTANTS
# ============================================================

PENDING = "Pending"
UNREACHABLE = "No response"
INVENTORYING = "Reading inventory"
INVENTORIED = "Inventoried"
PREPARING = "Preparing"
PREPARED = "Ready to reload"
SKIPPED = "Skipped"
FAILED = "Failed"
RELOADING = "Reloading"
WAITING = "Waiting for boot"
DONE = "Upgraded"
COLLECTING = "Collecting files"
COLLECTED = "Files saved"


# ============================================================
# CONFIG CONTAINERS
# ============================================================

@dataclass
class ImageSpec:
    """One image target, keyed by model PID prefix."""
    image: str
    md5: str
    target_version: str
    size_kb: int


@dataclass
class UpgradeConfig:
    """
    Credentials are the only required fields. tftp_server and
    family_images are needed by the upgrade path only, so inventory and
    file collection can run without them.
    """
    username: str
    password: str
    tftp_server: str = ""
    family_images: dict = field(default_factory=dict)
    disk_buffer_kb: int = 50000
    reboot_timeout: int = 1200
    reboot_poll_interval: int = 15
    copy_timeout: int = 2400
    # rough expectation used only to animate the copy progress bar
    expected_copy_seconds: int = 540

    # --- connection timeouts ---
    # The upgrade path talks to switches we know are there, so it waits
    # patiently. Scanning is the opposite problem: most addresses in a
    # subnet are dead, and every second spent on one is a second the
    # live switches wait.
    session_timeout: int = 30
    scan_timeout: int = 12

    # --- reachability probe ---
    # A TCP connect to the SSH port answers "is anything there?" in
    # milliseconds. Without it a dead address costs the full TCP SYN
    # timeout (tens of seconds) before netmiko gives up.
    probe_first: bool = True
    probe_port: int = 22
    probe_timeout: float = 1.5

    # --- file collection ---
    output_dir: str = ""
    tech_read_timeout: int = 1800

    def device_args(self, ip, timeout=None):
        seconds = timeout or self.session_timeout
        return {
            "device_type": "cisco_ios",
            "host": ip,
            "username": self.username,
            "password": self.password,
            "timeout": seconds,
            "auth_timeout": seconds,
            "banner_timeout": seconds,
        }


@dataclass
class SwitchState:
    """Everything known about one switch. The GUI renders straight from this."""
    ip: str
    hostname: str = ""
    model: str = ""
    serial: str = ""
    mac: str = ""
    boot_mode: str = ""
    current_version: str = ""
    target_version: str = ""
    image: str = ""
    status: str = PENDING
    progress: int = 0
    message: str = ""
    boot_line: str = ""
    stack_members: list = field(default_factory=list)
    log_lines: list = field(default_factory=list)


# ============================================================
# REPORTER
# ============================================================

class Reporter:
    """
    Wraps the caller's update callback. The callback receives
    (ip, dict_of_changed_fields) and is responsible for making it
    thread-safe - the GUI pushes onto a queue, a CLI can just print.
    """

    def __init__(self, callback):
        self._callback = callback

    def update(self, ip, **fields):
        if self._callback:
            self._callback(ip, fields)

    def log(self, ip, text):
        if self._callback:
            self._callback(ip, {"log_append": text})


# ============================================================
# LOW-LEVEL HELPERS
# ============================================================

def tcp_probe(ip, port=22, timeout=1.5):
    """
    True when something accepts a TCP connection on the given port.

    Used to skip dead addresses fast when scanning a subnet. A refused
    connection returns immediately; only a silently dropped SYN costs
    the full timeout.
    """
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def safe_filename(name, fallback="switch"):
    """Strips anything a filesystem would object to out of a hostname."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip()).strip("._-")
    return cleaned or fallback


def is_reachable(ip):
    is_windows = platform.system().lower() == "windows"
    cmd = (["ping", "-n", "1", "-w", "1000", ip] if is_windows
           else ["ping", "-c", "1", "-W", "1", ip])
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def get_model_pid(conn):
    output = conn.send_command("show inventory", read_timeout=30)
    match = re.search(
        r'NAME:\s*"(?:Switch\s*)?\d+"\s*,\s*DESCR:\s*"[^"]*"\s*\r?\n\s*PID:\s*(\S+)',
        output,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def resolve_image(pid, family_images):
    if not pid:
        return None
    for prefix, spec in family_images.items():
        if pid.startswith(prefix):
            return spec
    return None


def get_current_version(conn):
    output = conn.send_command("show version", read_timeout=30)
    match = re.search(r"Version\s+([\w.()]+)", output)
    return match.group(1) if match else None


def get_free_flash_kb(conn):
    output = conn.send_command("dir flash: | include bytes free", read_timeout=30)
    match = re.search(r"\(([\d]+)\s+bytes free\)", output)
    return int(match.group(1)) // 1024 if match else None


# ============================================================
# INVENTORY PARSERS
# ============================================================

def parse_inventory_members(output):
    """
    Returns {member_number: {"model": PID, "serial": SN}} from show inventory.

    Chassis entries look like:
        NAME: "Switch 1", DESCR: "C9200L-48P-4G ..."
        PID: C9200L-48P-4G     , VID: V01  , SN: JAE12345678

    Sub-components (power supplies, fans, uplink modules) have longer NAME
    values and are deliberately not matched.
    """
    members = {}
    pattern = re.compile(
        r'NAME:\s*"(?:Switch\s*)?(\d+)"\s*,\s*DESCR:\s*"[^"]*"\s*\r?\n'
        r'\s*PID:\s*(\S+)\s*,\s*VID:\s*(\S*)\s*,\s*SN:\s*(\S+)',
        re.IGNORECASE,
    )
    for member, pid, _vid, serial in pattern.findall(output):
        members[member] = {"model": pid, "serial": serial}
    return members


def parse_version_table(output):
    """
    Returns {member_number: {"model", "version", "mode"}} from the switch
    table at the bottom of show version:

        Switch Ports Model            SW Version   SW Image           Mode
        ------ ----- -----            ----------   ----------         ----
        *    1 52    C9200L-48P-4G    17.15.05     CAT9K_LITE_IOSXE   BUNDLE
    """
    members = {}
    pattern = re.compile(
        r"^\*?\s*(\d+)\s+\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$",
        re.MULTILINE,
    )
    for member, model, version, _image, mode in pattern.findall(output):
        members[member] = {"model": model, "version": version, "mode": mode}
    return members


def parse_base_mac(output):
    match = re.search(
        r"Base [Ee]thernet MAC [Aa]ddress\s*:\s*([0-9A-Fa-f:.]+)", output)
    return match.group(1) if match else ""


def parse_system_serial(output):
    match = re.search(r"System [Ss]erial [Nn]umber\s*:\s*(\S+)", output)
    return match.group(1) if match else ""


def parse_boot_mode(output):
    """INSTALL when booted from packages.conf, otherwise BUNDLE."""
    match = re.search(r'System image file is\s+"([^"]+)"', output)
    if not match:
        return ""
    return "INSTALL" if "packages.conf" in match.group(1).lower() else "BUNDLE"


# ============================================================
# INVENTORY COLLECTION (read-only, changes nothing)
# ============================================================

def collect_inventory(ip, config, reporter, cancel_check=None):
    """
    Reads hostname, model, serial, version, MAC and boot mode from one
    switch. Read-only - runs no config commands and changes nothing,
    and needs no TFTP server or image configuration.

    Stacks populate state.stack_members with one entry per member; the
    top-level fields reflect member 1.

    cancel_check: optional callable returning True to abort early.
    """
    state = SwitchState(ip=ip, status=INVENTORYING)

    if cancel_check is not None and cancel_check():
        state.status = SKIPPED
        state.message = "Cancelled"
        reporter.update(ip, status=SKIPPED, progress=0, message="Cancelled")
        return state

    # Dead addresses are the common case when scanning a subnet, so they
    # are settled by a fast TCP probe rather than by waiting for the SSH
    # handshake to time out.
    if config.probe_first:
        reporter.update(ip, status=INVENTORYING, progress=5, message="Probing...")
        if not tcp_probe(ip, config.probe_port, config.probe_timeout):
            state.status = UNREACHABLE
            state.message = f"No answer on port {config.probe_port}"
            reporter.update(ip, status=UNREACHABLE, progress=0, message=state.message)
            return state

    reporter.update(ip, status=INVENTORYING, progress=10, message="Connecting...")

    try:
        with ConnectHandler(**config.device_args(ip, timeout=config.scan_timeout)) as conn:
            state.hostname = conn.find_prompt().strip("#>")
            reporter.update(ip, hostname=state.hostname, progress=35,
                            message="Reading show version...")

            version_out = conn.send_command("show version", read_timeout=60)
            reporter.update(ip, progress=65, message="Reading show inventory...")
            inventory_out = conn.send_command("show inventory", read_timeout=60)

            inv_members = parse_inventory_members(inventory_out)
            ver_members = parse_version_table(version_out)

            base_mac = parse_base_mac(version_out)
            system_serial = parse_system_serial(version_out)
            boot_mode = parse_boot_mode(version_out)

            global_version = ""
            m = re.search(r"Cisco IOS XE Software, Version\s+([\w.()]+)", version_out)
            if m:
                global_version = m.group(1)
            else:
                m = re.search(r"Version\s+([\w.()]+)", version_out)
                global_version = m.group(1) if m else ""

            member_ids = sorted(
                set(inv_members) | set(ver_members),
                key=lambda x: int(x) if x.isdigit() else 0,
            ) or ["1"]

            members = []
            for member in member_ids:
                inv = inv_members.get(member, {})
                ver = ver_members.get(member, {})
                members.append({
                    "ip": ip,
                    "hostname": state.hostname,
                    "stack_member": member,
                    "model": inv.get("model") or ver.get("model", ""),
                    "serial": inv.get("serial") or (system_serial if member == "1" else ""),
                    "version": ver.get("version") or global_version,
                    "mac": base_mac if member == "1" else "",
                    "boot_mode": ver.get("mode") or boot_mode,
                })

            state.stack_members = members
            first = members[0]
            state.model = first["model"]
            state.serial = first["serial"]
            state.current_version = first["version"]
            state.mac = first["mac"]
            state.boot_mode = first["boot_mode"]
            state.status = INVENTORIED

            note = f"{len(members)} stack members" if len(members) > 1 else state.boot_mode
            state.message = note

            reporter.update(
                ip, status=INVENTORIED, progress=100,
                model=state.model, serial=state.serial, mac=state.mac,
                boot_mode=state.boot_mode, current_version=state.current_version,
                stack_members=members, message=note,
            )
            return state

    except Exception as e:
        state.status = FAILED
        state.message = str(e)
        reporter.update(ip, status=FAILED, progress=0, message=str(e))
        return state


# ============================================================
# FILE COLLECTION (read-only: show running-config / show tech-support)
# ============================================================

def _write_output(directory, hostname, ip, text):
    """
    Writes one capture, keeping the <hostname>.txt naming from the
    standalone export script. Two switches can answer to the same
    hostname, so a name that is already taken gets the IP appended
    rather than silently overwriting the earlier file.
    """
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{hostname}.txt")
    if os.path.exists(path):
        path = os.path.join(directory, f"{hostname}_{ip}.txt")
    # errors="replace" so one odd byte in a tech-support dump cannot
    # throw away the whole capture.
    with open(path, "w", encoding="utf-8", errors="replace", newline="") as f:
        f.write(text)
    return path


def collect_files(ip, config, reporter, want_run=True, want_tech=True,
                  cancel_check=None, hostname_hint=""):
    """
    Saves "show running-config" and/or "show tech-support" from one
    switch into config.output_dir. Read-only - runs no config commands.

    Files land in <output_dir>/configs/<hostname>.txt and
    <output_dir>/tech-support/<hostname>.txt.

    Returns {"ip", "hostname", "ok", "files", "error"}.
    """
    result = {"ip": ip, "hostname": hostname_hint, "ok": False,
              "files": [], "error": ""}

    def fail(msg):
        result["error"] = msg
        reporter.update(ip, status=FAILED, progress=0, message=msg)
        return result

    if not want_run and not want_tech:
        return fail("Nothing selected to collect")
    if not config.output_dir:
        return fail("No output folder configured")

    if cancel_check is not None and cancel_check():
        reporter.update(ip, status=SKIPPED, progress=0, message="Cancelled")
        result["error"] = "Cancelled"
        return result

    reporter.update(ip, status=COLLECTING, progress=5, message="Connecting...")

    if config.probe_first and not tcp_probe(ip, config.probe_port, config.probe_timeout):
        reporter.update(ip, status=UNREACHABLE, progress=0,
                        message=f"No answer on port {config.probe_port}")
        result["error"] = "unreachable"
        return result

    try:
        with ConnectHandler(**config.device_args(ip)) as conn:
            hostname = hostname_hint or conn.find_prompt().strip("#>")
            hostname = safe_filename(hostname, fallback=ip.replace(".", "-"))
            result["hostname"] = hostname
            reporter.update(ip, hostname=hostname, progress=15, message="Connected")

            if want_run:
                if cancel_check is not None and cancel_check():
                    reporter.update(ip, status=SKIPPED, message="Cancelled")
                    result["error"] = "Cancelled"
                    return result

                reporter.update(ip, progress=25, message="Pulling running-config...")
                running = conn.send_command("show running-config", read_timeout=180)
                path = _write_output(os.path.join(config.output_dir, "configs"),
                                     hostname, ip, running)
                result["files"].append(path)
                reporter.log(ip, f"Saved running-config: {path}")
                reporter.update(ip, progress=45, message="Running-config saved")

            if want_tech:
                if cancel_check is not None and cancel_check():
                    reporter.update(ip, status=SKIPPED, message="Cancelled")
                    result["error"] = "Cancelled"
                    return result

                # show tech-support runs for minutes and produces tens of
                # MB. send_command_timing reads until the device goes
                # quiet for last_read seconds rather than trying to match
                # a prompt inside output that contains prompt-like text.
                reporter.update(ip, progress=55,
                                message="Pulling tech-support (several minutes)...")
                started = time.time()
                tech = conn.send_command_timing(
                    "show tech-support",
                    read_timeout=config.tech_read_timeout,
                    last_read=5.0,
                )
                path = _write_output(os.path.join(config.output_dir, "tech-support"),
                                     hostname, ip, tech)
                result["files"].append(path)
                elapsed = int(time.time() - started)
                reporter.log(ip, f"Saved tech-support after {elapsed // 60}m"
                                 f"{elapsed % 60:02d}s: {path}")

            result["ok"] = True
            saved = "config + tech-support" if (want_run and want_tech) else (
                "running-config" if want_run else "tech-support")
            reporter.update(ip, status=COLLECTED, progress=100,
                            message=f"Saved {saved}")
            return result

    except Exception as e:
        return fail(str(e))


# ============================================================
# STEP: install remove inactive
# ============================================================

def install_remove_inactive(conn, ip, reporter, timeout=600):
    """
    Runs "install remove inactive", auto-answering its [y/n] prompt.

    Runs BEFORE the new image is copied - this command deletes unused
    .bin files from flash, so running it afterward would delete the
    image that was just transferred.
    """
    reporter.log(ip, "install remove inactive")
    conn.write_channel("install remove inactive\n")
    output = ""
    start = time.time()
    answered = False

    while time.time() - start < timeout:
        time.sleep(5)
        try:
            chunk = conn.read_channel()
        except Exception:
            break
        if chunk:
            output += chunk
        if not answered and "[y/n]" in output.lower():
            conn.write_channel("y\n")
            answered = True
        if "SUCCESS: install_remove" in output or "install_remove: END" in output:
            break
        if "FAILED: install_remove" in output:
            reporter.log(ip, "install remove inactive reported a failure (continuing)")
            break
        if "nothing to clean" in output.lower() or "no inactive packages" in output.lower():
            break

    return output


# ============================================================
# STEP: copy image from TFTP
# ============================================================

def copy_image_to_flash(conn, ip, spec, config, reporter,
                        progress_from=30, progress_to=70):
    """
    Copies the image from TFTP to flash as its own explicit step so the
    MD5 can be checked before anything else touches the file.

    The progress bar between progress_from and progress_to is an elapsed
    time estimate, not real transfer telemetry - IOS doesn't report a
    percentage over this channel. The elapsed seconds in the message are
    real.

    Returns (output, status): True copied, False error, None timed out.
    """
    conn.write_channel(f"copy tftp://{config.tftp_server}/{spec.image} flash:\n")
    output = ""
    start = time.time()
    confirmed_destination = False
    span = progress_to - progress_from

    while time.time() - start < config.copy_timeout:
        time.sleep(5)
        elapsed = time.time() - start

        fraction = min(elapsed / max(config.expected_copy_seconds, 1), 0.97)
        reporter.update(
            ip,
            progress=int(progress_from + span * fraction),
            message=f"Copying image... {int(elapsed // 60)}m{int(elapsed % 60):02d}s elapsed",
        )

        try:
            chunk = conn.read_channel()
        except Exception as e:
            return output + f"\n[read error: {e}]", False
        if chunk:
            output += chunk

        # IOS prompts for the destination filename - accept the default
        if not confirmed_destination and "Destination filename" in output:
            conn.write_channel("\n")
            confirmed_destination = True

        if "bytes copied in" in output:
            return output, True
        if "%Error" in output or "Error copying" in output or "Timed out" in output:
            return output, False

    return output, None


# ============================================================
# STEP: verify MD5
# ============================================================

def image_exists_on_flash(conn, image):
    """
    Returns (exists, size_bytes) for an image already sitting on flash.
    Size is used as a fallback sanity check when no expected MD5 is
    configured.
    """
    output = conn.send_command(f"dir flash:{image}", read_timeout=60)
    if "No such file" in output or "%Error" in output:
        return False, 0
    # e.g. "16216  -rw-  503981351  Aug 18 2026 13:45:48 -05:00  <name>"
    match = re.search(r"^\s*\d+\s+\S+\s+(\d+)\s", output, re.MULTILINE)
    if not match:
        return False, 0
    return True, int(match.group(1))


def delete_from_flash(conn, image):
    """Deletes a file with /force so it doesn't stop on a confirmation."""
    conn.send_command(f"delete /force flash:{image}", read_timeout=60,
                      expect_string=r"#")


def verify_md5(conn, spec):
    """
    Returns (actual_md5, matches). matches is None when no expected hash
    was configured, meaning verification was skipped rather than passed.
    """
    output = conn.send_command(f"verify /md5 flash:{spec.image}", read_timeout=300)
    match = re.search(r"=\s*([0-9a-fA-F]{32})", output)
    actual = match.group(1) if match else None

    expected = (spec.md5 or "").strip()
    if not expected or expected.upper().startswith("PUT_"):
        return actual, None

    return actual, (actual is not None and actual.lower() == expected.lower())


# ============================================================
# STEP: set and verify boot system
# ============================================================

def set_boot_system(conn, spec):
    """
    configure terminal / no boot system / boot system flash:<image> /
    end / write memory
    """
    conn.send_config_set([
        "no boot system",
        f"boot system flash:{spec.image}",
    ])
    conn.exit_config_mode()
    conn.save_config()


def verify_boot_system(conn, spec):
    """
    Confirms the new image is the ONLY boot entry, checking both
    "show boot" and "show run | include boot system".

    A leftover second entry lets the switch silently fall through to an
    old image on reload - that condition caused a boot loop during
    testing, so it is treated as fatal.

    Returns (ok, boot_value, reason).
    """
    show_boot = conn.send_command("show boot", read_timeout=30)
    show_run = conn.send_command("show run | include boot system", read_timeout=30)

    match = re.search(r"BOOT variable\s*=\s*([^\r\n]*)", show_boot)
    if not match:
        return False, "", "could not parse BOOT variable"

    boot_value = match.group(1).strip()
    entries = [e.strip() for e in boot_value.split(";") if e.strip()]

    if spec.image not in boot_value:
        return False, boot_value, f"BOOT variable does not reference {spec.image}"
    if len(entries) > 1:
        return False, boot_value, f"BOOT variable has {len(entries)} entries - could fall through to an old image"
    if spec.image not in show_run:
        return False, boot_value, "running-config boot statement does not match the new image"

    return True, boot_value, ""


# ============================================================
# PHASE 1: PREPARE (never reloads)
# ============================================================

def prepare_switch(ip, config, reporter, cancel_check=None):
    """
    Runs the full prepare sequence on one switch. Returns a SwitchState.
    Nothing here reloads the switch.

    cancel_check: optional callable returning True to abort early.
    """
    state = SwitchState(ip=ip, status=PREPARING)

    def cancelled():
        return cancel_check is not None and cancel_check()

    def fail(msg):
        state.status = FAILED
        state.message = msg
        reporter.update(ip, status=FAILED, message=msg, progress=0)
        return state

    try:
        reporter.update(ip, status=PREPARING, progress=2, message="Connecting...")
        with ConnectHandler(**config.device_args(ip)) as conn:
            state.hostname = conn.find_prompt().strip("#>")
            reporter.update(ip, hostname=state.hostname, progress=5, message="Connected")

            if cancelled():
                return fail("Cancelled")

            # --- identify model ---
            pid = get_model_pid(conn)
            state.model = pid or "unknown"
            spec = resolve_image(pid, config.family_images)
            if not spec:
                return fail(f"Unrecognized model '{pid}' - no image configured")
            state.image = spec.image
            state.target_version = spec.target_version
            reporter.update(ip, model=state.model, image=spec.image,
                            target_version=spec.target_version,
                            progress=10, message=f"Model {pid}")

            # --- current version ---
            state.current_version = get_current_version(conn) or ""
            reporter.update(ip, current_version=state.current_version,
                            progress=15, message=f"Running {state.current_version}")

            if state.current_version and spec.target_version in state.current_version:
                state.status = SKIPPED
                state.message = f"Already on {spec.target_version}"
                state.progress = 100
                reporter.update(ip, status=SKIPPED, progress=100,
                                message=f"Already on {spec.target_version}")
                return state

            if cancelled():
                return fail("Cancelled")

            # --- is a usable copy already on flash? ---
            # This runs BEFORE "install remove inactive", because that
            # command deletes unused .bin files from flash - running it
            # first would delete the very copy we want to reuse.
            reporter.update(ip, progress=18, message="Checking flash for existing image...")
            exists, size_bytes = image_exists_on_flash(conn, spec.image)
            need_copy = True

            if exists:
                expected_bytes = spec.size_kb * 1024
                reporter.log(ip, f"Found {spec.image} on flash ({size_bytes:,} bytes)")
                reporter.update(ip, progress=20, message="Image on flash, verifying MD5...")

                actual, matches = verify_md5(conn, spec)

                if matches is True:
                    need_copy = False
                    reporter.log(ip, f"Existing copy verified ({actual}) - skipping TFTP transfer")
                    reporter.update(ip, progress=85,
                                    message="Existing image verified - transfer skipped")
                elif matches is False:
                    reporter.log(ip, f"Existing copy FAILED MD5 (got {actual}) - deleting and re-transferring")
                    delete_from_flash(conn, spec.image)
                else:
                    # No expected hash configured. Fall back to an exact size
                    # match - the truncated transfer that caused a boot loop
                    # during testing was 729 bytes short, so size alone does
                    # catch that case.
                    if size_bytes == expected_bytes:
                        need_copy = False
                        reporter.log(ip, f"No MD5 configured; size matches exactly "
                                         f"({size_bytes:,} bytes) - skipping TFTP transfer")
                        reporter.update(ip, progress=85,
                                        message="Size matched (MD5 not configured) - transfer skipped")
                    else:
                        reporter.log(ip, f"No MD5 configured and size differs "
                                         f"(on flash {size_bytes:,}, expected {expected_bytes:,}) "
                                         f"- deleting and re-transferring")
                        delete_from_flash(conn, spec.image)

            if need_copy:
                # --- install remove inactive (frees space before the copy) ---
                reporter.update(ip, progress=22, message="Clearing inactive packages...")
                install_remove_inactive(conn, ip, reporter)
                reporter.update(ip, progress=25, message="Inactive packages cleared")

                # --- free space ---
                free_kb = get_free_flash_kb(conn)
                required_kb = spec.size_kb + config.disk_buffer_kb
                if free_kb is None:
                    reporter.log(ip, "Could not read free flash space - continuing")
                elif free_kb < required_kb:
                    return fail(f"Insufficient flash: {free_kb:,} KB free, need {required_kb:,} KB")
                else:
                    reporter.log(ip, f"Flash OK: {free_kb:,} KB free, need {required_kb:,} KB")
                reporter.update(ip, progress=30, message="Flash space OK")

                if cancelled():
                    return fail("Cancelled")

                # --- copy from TFTP ---
                output, copy_status = copy_image_to_flash(conn, ip, spec, config, reporter)
                if copy_status is None:
                    return fail("TFTP copy timed out")
                if copy_status is False:
                    tail = output[-200:].replace("\n", " ")
                    return fail(f"TFTP copy failed: {tail}")
                reporter.update(ip, progress=72, message="Transfer complete, verifying MD5...")

                # --- verify MD5 of the fresh copy ---
                actual, matches = verify_md5(conn, spec)
                if matches is False:
                    return fail(f"MD5 mismatch after transfer - got {actual}, expected {spec.md5}")
                if matches is None:
                    reporter.log(ip, f"MD5 NOT verified (no expected hash configured) - got {actual}")
                    reporter.update(ip, progress=85, message="MD5 not verified - no hash configured")
                else:
                    reporter.log(ip, f"MD5 verified: {actual}")
                    reporter.update(ip, progress=85, message="MD5 verified")

            if cancelled():
                return fail("Cancelled")

            # --- set boot system ---
            reporter.update(ip, progress=90, message="Setting boot system...")
            set_boot_system(conn, spec)

            # --- verify boot system ---
            ok, boot_value, reason = verify_boot_system(conn, spec)
            state.boot_line = boot_value
            reporter.update(ip, boot_line=boot_value)
            reporter.log(ip, f"BOOT variable: {boot_value}")

            if not ok:
                return fail(f"Boot verification failed: {reason}")

            state.status = PREPARED
            state.progress = 100
            state.message = "Ready to reload"
            reporter.update(ip, status=PREPARED, progress=100, message="Ready to reload")
            return state

    except Exception as e:
        return fail(str(e))


# ============================================================
# PHASE 2: RELOAD
# ============================================================

def reload_switch(state, config, reporter):
    """
    Reloads one prepared switch, waits for it to come back, and confirms
    the running version. Mutates and returns the given SwitchState.
    """
    ip = state.ip
    hostname = state.hostname

    reporter.update(ip, status=RELOADING, progress=0, message="Sending reload...")
    try:
        with ConnectHandler(**config.device_args(ip)) as conn:
            conn.write_channel("reload\n")
            time.sleep(3)
            out = conn.read_channel()
            if "[confirm]" in out.lower() or "[yes/no]" in out.lower():
                conn.write_channel("\n")
                time.sleep(2)
                out += conn.read_channel()
            if "[confirm]" in out.lower():
                conn.write_channel("\n")
    except Exception:
        pass  # the connection dropping is the expected outcome

    reporter.update(ip, status=WAITING, message="Waiting for switch to come back...")
    waited = 0
    back = False
    while waited < config.reboot_timeout:
        time.sleep(config.reboot_poll_interval)
        waited += config.reboot_poll_interval
        pct = int(min(waited / config.reboot_timeout, 0.95) * 100)
        reporter.update(ip, progress=pct,
                        message=f"Waiting for boot... {waited // 60}m{waited % 60:02d}s")
        if is_reachable(ip):
            back = True
            break

    if not back:
        state.status = FAILED
        state.message = f"Did not come back within {config.reboot_timeout}s"
        reporter.update(ip, status=FAILED, progress=0, message=state.message)
        return state

    reporter.update(ip, message="Ping OK, waiting for SSH...")
    time.sleep(30)

    try:
        with ConnectHandler(**config.device_args(ip)) as conn:
            new_version = get_current_version(conn) or ""
            state.current_version = new_version
            if state.target_version and state.target_version in new_version:
                state.status = DONE
                state.progress = 100
                state.message = f"Upgraded to {new_version}"
                reporter.update(ip, status=DONE, progress=100,
                                current_version=new_version,
                                message=f"Upgraded to {new_version}")
            else:
                state.status = FAILED
                state.message = f"Came back on {new_version}, expected {state.target_version}"
                reporter.update(ip, status=FAILED, progress=100,
                                current_version=new_version, message=state.message)
    except Exception as e:
        state.status = FAILED
        state.message = f"Ping OK but SSH failed: {e}"
        reporter.update(ip, status=FAILED, message=state.message)

    return state
