"""
switch_upgrade_gui.py - Tkinter front end for the IOS-XE bulk upgrade tool.

Run:      python switch_upgrade_gui.py
Build:    pyinstaller --onefile --windowed switch_upgrade_gui.py

Threading model: all network work happens on worker threads. Those threads
never touch widgets - they push messages onto a queue, and the UI thread
drains that queue on a timer. Tkinter is not thread-safe, so this
separation is required, not stylistic.
"""

import csv
import os
import ipaddress
import queue
import threading
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from concurrent.futures import ThreadPoolExecutor

import upgrade_engine as engine


# Shown greyed out above the image table. Nothing here is filled in for
# you - an image, hash and size left over from someone else's upgrade is
# worse than an empty box, because it looks deliberate.
IMAGE_EXAMPLE = ("Example:   C9200   |   9200_cat9k_lite_iosxe.17.18.04.SPA.bin   |   "
                 "32-character MD5 from the Cisco download page   |   17.18.04   |   "
                 "size in KB, from dir flash:")

# How many blank rows the image table opens with.
INITIAL_IMAGE_ROWS = 2

# Shown in the method picker; mapped back to the engine constants.
METHOD_LABELS = {
    engine.METHOD_BUNDLE: "Boot system (bundle) - validated",
    engine.METHOD_INSTALL: "install add / activate / commit",
    engine.METHOD_MATCH: "Match each switch's current mode",
}
METHOD_BY_LABEL = {label: method for method, label in METHOD_LABELS.items()}

STATUS_COLORS = {
    engine.PENDING: "#666666",
    engine.UNREACHABLE: "#999999",
    engine.COLLECTING: "#0066cc",
    engine.COLLECTED: "#008800",
    engine.INVENTORYING: "#0066cc",
    engine.INVENTORIED: "#005599",
    engine.PREPARING: "#0066cc",
    engine.PREPARED: "#cc7700",
    engine.SKIPPED: "#666666",
    engine.FAILED: "#cc0000",
    engine.RELOADING: "#cc7700",
    engine.WAITING: "#cc7700",
    engine.DONE: "#008800",
}


class SwitchRow:
    """One row in the switch table: checkbox, labels, progress bar, button."""

    def __init__(self, parent, ip, row_index, on_reload):
        self.ip = ip
        self.on_reload = on_reload
        self.state = engine.SwitchState(ip=ip)

        self.selected = tk.BooleanVar(value=True)
        self.check = ttk.Checkbutton(parent, variable=self.selected)
        self.check.grid(row=row_index, column=0, padx=(4, 0), pady=2)

        self.lbl_ip = ttk.Label(parent, text=ip, width=14, anchor="w")
        self.lbl_ip.grid(row=row_index, column=1, sticky="w", padx=2)

        self.lbl_host = ttk.Label(parent, text="-", width=17, anchor="w")
        self.lbl_host.grid(row=row_index, column=2, sticky="w", padx=2)

        self.lbl_model = ttk.Label(parent, text="-", width=15, anchor="w")
        self.lbl_model.grid(row=row_index, column=3, sticky="w", padx=2)

        self.lbl_serial = ttk.Label(parent, text="-", width=13, anchor="w")
        self.lbl_serial.grid(row=row_index, column=4, sticky="w", padx=2)

        self.lbl_version = ttk.Label(parent, text="-", width=10, anchor="w")
        self.lbl_version.grid(row=row_index, column=5, sticky="w", padx=2)

        self.lbl_mac = ttk.Label(parent, text="-", width=15, anchor="w")
        self.lbl_mac.grid(row=row_index, column=6, sticky="w", padx=2)

        self.lbl_mode = ttk.Label(parent, text="-", width=8, anchor="w")
        self.lbl_mode.grid(row=row_index, column=7, sticky="w", padx=2)

        self.lbl_status = ttk.Label(parent, text=engine.PENDING, width=15,
                                    anchor="w", foreground=STATUS_COLORS[engine.PENDING])
        self.lbl_status.grid(row=row_index, column=8, sticky="w", padx=2)

        self.progress = ttk.Progressbar(parent, length=110, mode="determinate", maximum=100)
        self.progress.grid(row=row_index, column=9, padx=4)

        self.lbl_message = ttk.Label(parent, text="", width=28, anchor="w")
        self.lbl_message.grid(row=row_index, column=10, sticky="w", padx=2)

        self.btn_reload = ttk.Button(parent, text="Reload", width=8,
                                     command=self._reload_clicked, state="disabled")
        self.btn_reload.grid(row=row_index, column=11, padx=(2, 6))

    @property
    def widgets(self):
        return (self.check, self.lbl_ip, self.lbl_host, self.lbl_model,
                self.lbl_serial, self.lbl_version, self.lbl_mac, self.lbl_mode,
                self.lbl_status, self.progress, self.lbl_message, self.btn_reload)

    def _reload_clicked(self):
        self.on_reload(self)

    def apply(self, fields):
        """Applies a dict of changed fields from the worker thread."""
        if "log_append" in fields:
            self.state.log_lines.append(fields["log_append"])
            return

        for key, value in fields.items():
            if hasattr(self.state, key):
                setattr(self.state, key, value)

        if "hostname" in fields:
            self.lbl_host.config(text=fields["hostname"] or "-")
        if "model" in fields:
            self.lbl_model.config(text=fields["model"] or "-")
        if "serial" in fields:
            self.lbl_serial.config(text=fields["serial"] or "-")
        if "current_version" in fields:
            self.lbl_version.config(text=fields["current_version"] or "-")
        if "mac" in fields:
            self.lbl_mac.config(text=fields["mac"] or "-")
        if "boot_mode" in fields:
            # Informational only. The boot system workflow this tool uses
            # (copy to flash -> boot system flash:<img> -> verify -> reload)
            # works the same in BUNDLE or INSTALL mode. Mode only matters
            # for the install add/activate/commit workflow, which this
            # tool does not use.
            self.lbl_mode.config(text=fields["boot_mode"] or "-")
        if "progress" in fields:
            self.progress["value"] = fields["progress"]
        if "message" in fields:
            self.lbl_message.config(text=fields["message"])
        if "status" in fields:
            status = fields["status"]
            self.lbl_status.config(text=status,
                                   foreground=STATUS_COLORS.get(status, "#000000"))
            # The reload button only unlocks once boot has been verified
            self.btn_reload.config(
                state="normal" if status == engine.PREPARED else "disabled"
            )

    def set_button_enabled(self, enabled):
        if self.state.status == engine.PREPARED:
            self.btn_reload.config(state="normal" if enabled else "disabled")

    def set_visible(self, visible):
        """grid_remove keeps the row's grid position for when it comes back."""
        for widget in self.widgets:
            if visible:
                widget.grid()
            else:
                widget.grid_remove()


class UpgradeApp:

    def __init__(self, root):
        self.root = root
        root.title("Cisco IOS-XE Bulk Upgrade")
        root.geometry("1250x860")

        self.msg_queue = queue.Queue()
        self.rows = {}          # ip -> SwitchRow
        self.busy = False          # a batch phase owns the whole window
        self.inventory_ran = False
        self._reloading = set()    # switches with their own reload in flight
        self._workers = 3
        self._scan_workers = 20
        self.cancel_flag = threading.Event()

        self._build_config_panel()
        self._build_switch_panel()
        self._build_log_panel()
        self._build_action_bar()

        self.root.after(100, self._drain_queue)

    # --------------------------------------------------------
    # UI construction
    # --------------------------------------------------------

    def _build_config_panel(self):
        frame = ttk.LabelFrame(self.root, text="Connection")
        frame.pack(fill="x", padx=8, pady=(8, 4))

        # Credentials are all that inventory and file collection need.
        ttk.Label(frame, text="Username:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.var_user = tk.StringVar()
        ttk.Entry(frame, textvariable=self.var_user, width=18).grid(row=0, column=1, padx=4)

        ttk.Label(frame, text="Password:").grid(row=0, column=2, sticky="e", padx=4)
        self.var_pass = tk.StringVar()
        ttk.Entry(frame, textvariable=self.var_pass, width=18, show="*").grid(row=0, column=3, padx=4)

        ttk.Label(frame, text="TFTP server:").grid(row=0, column=4, sticky="e", padx=4)
        self.var_tftp = tk.StringVar()
        ttk.Entry(frame, textvariable=self.var_tftp, width=18).grid(row=0, column=5, padx=4)
        ttk.Label(frame, text="(upgrades only)", foreground="#666666").grid(
            row=0, column=6, sticky="w", padx=(0, 4))

        ttk.Label(frame, text="Prepare workers:").grid(row=1, column=0, sticky="e", padx=4, pady=(0, 6))
        self.var_workers = tk.StringVar(value="3")
        ttk.Spinbox(frame, from_=1, to=10, textvariable=self.var_workers,
                    width=5).grid(row=1, column=1, sticky="w", padx=4)

        ttk.Label(frame, text="Scan workers:").grid(row=1, column=2, sticky="e", padx=4)
        self.var_scan_workers = tk.StringVar(value="20")
        ttk.Spinbox(frame, from_=1, to=100, textvariable=self.var_scan_workers,
                    width=5).grid(row=1, column=3, sticky="w", padx=4)

        ttk.Label(frame, text="Probe timeout (s):").grid(row=1, column=4, sticky="e", padx=4)
        self.var_probe = tk.StringVar(value="1.5")
        ttk.Spinbox(frame, from_=0.5, to=10.0, increment=0.5, format="%.1f",
                    textvariable=self.var_probe, width=5).grid(row=1, column=5, sticky="w", padx=4)

        ttk.Label(frame, text="Concurrent TFTP transfers:").grid(
            row=2, column=0, columnspan=2, sticky="e", padx=4, pady=(0, 6))
        self.var_transfers = tk.StringVar(value="1")
        ttk.Spinbox(frame, from_=1, to=10, textvariable=self.var_transfers,
                    width=5).grid(row=2, column=2, sticky="w", padx=4, pady=(0, 6))
        ttk.Label(
            frame,
            text="TFTP is UDP with no congestion control - simultaneous transfers "
                 "cause loss and aborted sessions. Only the transfer queues; the "
                 "rest of prepare still runs in parallel.",
            foreground="#666666",
        ).grid(row=2, column=3, columnspan=5, sticky="w", padx=4, pady=(0, 6))

        ttk.Label(frame, text="Upgrade via:").grid(row=3, column=0, sticky="e",
                                                   padx=4, pady=(0, 6))
        self.var_method = tk.StringVar(value=METHOD_LABELS[engine.METHOD_BUNDLE])
        ttk.Combobox(frame, textvariable=self.var_method, width=34, state="readonly",
                     values=[METHOD_LABELS[m] for m in engine.UPGRADE_METHODS]).grid(
            row=3, column=1, columnspan=2, sticky="w", padx=4, pady=(0, 6))
        ttk.Label(
            frame,
            text="Bundle is the validated path. Install keeps a switch in INSTALL "
                 "mode (needed for SMU patching / ISSU).",
            foreground="#666666",
        ).grid(row=3, column=3, columnspan=5, sticky="w", padx=4, pady=(0, 6))

        self.var_hide_unreachable = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Hide non-responding rows",
                        variable=self.var_hide_unreachable,
                        command=self._apply_row_filter).grid(
            row=1, column=6, columnspan=2, sticky="w", padx=4)

        # --- file collection (show run / show tech-support) ---
        out_frame = ttk.LabelFrame(self.root, text="File collection (read-only)")
        out_frame.pack(fill="x", padx=8, pady=4)

        ttk.Label(out_frame, text="Save to:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.var_outdir = tk.StringVar()
        ttk.Entry(out_frame, textvariable=self.var_outdir, width=52).grid(row=0, column=1, padx=4)
        ttk.Button(out_frame, text="Browse...",
                   command=self._choose_output_dir).grid(row=0, column=2, padx=4)

        self.var_get_run = tk.BooleanVar(value=True)
        ttk.Checkbutton(out_frame, text="show running-config",
                        variable=self.var_get_run).grid(row=0, column=3, padx=(16, 4))

        self.var_get_tech = tk.BooleanVar(value=True)
        ttk.Checkbutton(out_frame, text="show tech-support (slow - minutes per switch)",
                        variable=self.var_get_tech).grid(row=0, column=4, padx=4)

        # --- image config, one row per family ---
        img_frame = ttk.LabelFrame(self.root, text="Images (matched by model PID prefix)")
        img_frame.pack(fill="x", padx=8, pady=4)

        self.img_frame = img_frame

        ttk.Label(img_frame, text=IMAGE_EXAMPLE, foreground="#888888").grid(
            row=0, column=0, columnspan=6, sticky="w", padx=4, pady=(4, 0))

        headers = ["Prefix", "Image filename", "Expected MD5", "Target version", "Size (KB)"]
        for col, text in enumerate(headers):
            ttk.Label(img_frame, text=text, font=("TkDefaultFont", 8, "bold")).grid(
                row=1, column=col, padx=4, sticky="w")

        # Rows are added and removed at run time, so the widgets below
        # them are re-gridded rather than pinned to a fixed row.
        self.image_rows = []
        self.btn_add_image = ttk.Button(img_frame, text="+  Add image",
                                        command=self._add_image_row, width=14)
        self.lbl_md5_note = ttk.Label(
            img_frame,
            text="Leave MD5 blank to skip verification (not recommended - a silent truncated "
                 "transfer is what causes corrupt installs). Size must match the image; it is "
                 "the fallback check when no MD5 is set.",
            foreground="#aa5500",
        )
        for _ in range(INITIAL_IMAGE_ROWS):
            self._add_image_row()

    # --------------------------------------------------------
    # Image rows
    # --------------------------------------------------------

    def _add_image_row(self):
        """Appends one blank image row and re-lays out what sits below it."""
        variables = {name: tk.StringVar() for name in
                     ("prefix", "image", "md5", "version", "size_kb")}

        widgets = [
            ttk.Entry(self.img_frame, textvariable=variables["prefix"], width=10),
            ttk.Entry(self.img_frame, textvariable=variables["image"], width=42),
            ttk.Entry(self.img_frame, textvariable=variables["md5"], width=34),
            ttk.Entry(self.img_frame, textvariable=variables["version"], width=12),
            ttk.Entry(self.img_frame, textvariable=variables["size_kb"], width=12),
        ]
        entry = {"vars": variables, "widgets": widgets}
        remove = ttk.Button(self.img_frame, text="\u2212", width=3,
                            command=lambda e=entry: self._remove_image_row(e))
        widgets.append(remove)

        self.image_rows.append(entry)
        self._regrid_image_rows()
        return entry

    def _remove_image_row(self, entry):
        """
        Drops one image row. The last row is kept so the table cannot
        disappear - it is cleared instead, which is the same thing an
        operator means by removing the only row.
        """
        if self.busy:
            return
        if len(self.image_rows) == 1:
            for var in entry["vars"].values():
                var.set("")
            return
        for widget in entry["widgets"]:
            widget.destroy()
        self.image_rows.remove(entry)
        self._regrid_image_rows()

    def _regrid_image_rows(self):
        """Closes the gap a removed row leaves and moves the footer down."""
        first_row = 2                      # after the example line and headers
        for index, entry in enumerate(self.image_rows):
            for col, widget in enumerate(entry["widgets"]):
                widget.grid(row=first_row + index, column=col, padx=4, pady=2,
                            sticky="w")

        footer = first_row + len(self.image_rows)
        self.btn_add_image.grid(row=footer, column=0, padx=4, pady=(4, 2), sticky="w")
        self.lbl_md5_note.grid(row=footer + 1, column=0, columnspan=6,
                               sticky="w", padx=4, pady=(2, 4))

    def _build_switch_panel(self):
        outer = ttk.LabelFrame(self.root, text="Switches")
        outer.pack(fill="both", expand=True, padx=8, pady=4)

        entry_bar = ttk.Frame(outer)
        entry_bar.pack(fill="x", padx=4, pady=4)

        ttk.Label(entry_bar,
                  text="IPs - one per line or comma separated. Ranges: "
                       "192.168.1.1-100, 192.168.1.1-192.168.1.50, 192.168.1.0/24"
                  ).pack(side="left")
        ttk.Button(entry_bar, text="Load from file...",
                   command=self._load_ips_from_file).pack(side="left", padx=6)
        ttk.Button(entry_bar, text="Build list",
                   command=self._build_switch_rows).pack(side="left")
        ttk.Button(entry_bar, text="Select all",
                   command=lambda: self._set_all_selected(True)).pack(side="left", padx=(12, 2))
        ttk.Button(entry_bar, text="Select none",
                   command=lambda: self._set_all_selected(False)).pack(side="left")

        self.txt_ips = tk.Text(outer, height=4, width=60)
        self.txt_ips.pack(fill="x", padx=4)

        # scrollable table area
        table_wrap = ttk.Frame(outer)
        table_wrap.pack(fill="both", expand=True, padx=4, pady=4)

        self.canvas = tk.Canvas(table_wrap, height=240)
        scrollbar = ttk.Scrollbar(table_wrap, orient="vertical", command=self.canvas.yview)
        self.table = ttk.Frame(self.canvas)

        self.table.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.table, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._build_table_headers()

    def _build_table_headers(self):
        headers = ["", "IP", "Hostname", "Model", "Serial", "Version",
                   "MAC", "Mode", "Status", "Progress", "Detail", ""]
        for col, text in enumerate(headers):
            ttk.Label(self.table, text=text,
                      font=("TkDefaultFont", 8, "bold")).grid(row=0, column=col, padx=2, sticky="w")

    def _build_log_panel(self):
        frame = ttk.LabelFrame(self.root, text="Log")
        frame.pack(fill="both", expand=False, padx=8, pady=4)

        self.txt_log = tk.Text(frame, height=9, wrap="none")
        log_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=log_scroll.set)
        self.txt_log.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        log_scroll.pack(side="right", fill="y", pady=4)

    def _build_action_bar(self):
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=8, pady=(0, 8))

        self.btn_inventory = ttk.Button(bar, text="Run Inventory (read-only)",
                                        command=self._start_inventory)
        self.btn_inventory.pack(side="left")

        self.btn_export = ttk.Button(bar, text="Export Inventory CSV",
                                     command=self._export_inventory, state="disabled")
        self.btn_export.pack(side="left", padx=6)

        self.btn_collect = ttk.Button(bar, text="Pull Configs / Tech-Support",
                                      command=self._start_collect)
        self.btn_collect.pack(side="left")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        self.btn_prepare = ttk.Button(bar, text="Start Prepare (no reloads)",
                                      command=self._start_prepare)
        self.btn_prepare.pack(side="left")

        self.btn_reload_selected = ttk.Button(bar, text="Reload Selected (one at a time)",
                                              command=lambda: self._start_reloads(parallel=False))
        self.btn_reload_selected.pack(side="left", padx=6)

        self.btn_reload_all = ttk.Button(bar, text="Reload Selected (all at once)",
                                         command=lambda: self._start_reloads(parallel=True))
        self.btn_reload_all.pack(side="left")

        self.btn_cancel = ttk.Button(bar, text="Cancel", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)

        self.lbl_summary = ttk.Label(bar, text="")
        self.lbl_summary.pack(side="right")

    # --------------------------------------------------------
    # Switch list handling
    # --------------------------------------------------------

    def _load_ips_from_file(self):
        path = filedialog.askopenfilename(
            title="Select a file with switch IPs",
            filetypes=[("Text files", "*.txt"), ("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r") as f:
                content = f.read()
            self.txt_ips.delete("1.0", "end")
            self.txt_ips.insert("1.0", content)
            self._build_switch_rows()
        except Exception as e:
            messagebox.showerror("Could not read file", str(e))

    def _expand_token(self, token):
        """
        Expands one token into a list of IPs. Accepts:
            192.168.1.45                single address
            192.168.1.1-100             last-octet range
            192.168.1.1-192.168.1.50    full start-end range
            192.168.1.0/24              CIDR (host addresses only)
        Returns [] for anything it can't parse, plus a reason.
        """
        token = token.strip()
        if not token:
            return [], None

        # CIDR
        if "/" in token:
            try:
                net = ipaddress.ip_network(token, strict=False)
                hosts = [str(h) for h in net.hosts()]
                if not hosts:
                    return [], f"{token} contains no host addresses"
                if len(hosts) > 1024:
                    return [], f"{token} expands to {len(hosts)} addresses - too many"
                return hosts, None
            except ValueError as e:
                return [], f"{token}: {e}"

        # Range
        if "-" in token:
            left, right = token.split("-", 1)
            left, right = left.strip(), right.strip()
            try:
                start = ipaddress.IPv4Address(left)
            except ValueError as e:
                return [], f"{token}: {e}"

            # "192.168.1.1-100" - right side is just the final octet
            if "." not in right:
                try:
                    last_octet = int(right)
                except ValueError:
                    return [], f"{token}: '{right}' is not a valid last octet"
                if not 0 <= last_octet <= 255:
                    return [], f"{token}: last octet {last_octet} out of range"
                prefix = ".".join(left.split(".")[:3])
                try:
                    end = ipaddress.IPv4Address(f"{prefix}.{last_octet}")
                except ValueError as e:
                    return [], f"{token}: {e}"
            else:
                try:
                    end = ipaddress.IPv4Address(right)
                except ValueError as e:
                    return [], f"{token}: {e}"

            if int(end) < int(start):
                return [], f"{token}: end address is before the start address"
            count = int(end) - int(start) + 1
            if count > 1024:
                return [], f"{token} expands to {count} addresses - too many"
            return [str(ipaddress.IPv4Address(i))
                    for i in range(int(start), int(end) + 1)], None

        # Single address
        try:
            ipaddress.IPv4Address(token)
            return [token], None
        except ValueError:
            return [], f"'{token}' is not a valid IP address"

    def _parse_ips(self, report_errors=False):
        raw = self.txt_ips.get("1.0", "end")
        tokens = [p for chunk in raw.splitlines() for p in chunk.split(",")]

        result = []
        errors = []
        for token in tokens:
            expanded, error = self._expand_token(token)
            if error:
                errors.append(error)
                continue
            for ip in expanded:
                if ip not in result:
                    result.append(ip)

        if report_errors and errors:
            messagebox.showwarning(
                "Some entries were skipped",
                "These entries could not be parsed:\n\n" + "\n".join(errors[:12])
                + ("\n..." if len(errors) > 12 else ""),
            )

        return result

    def _build_switch_rows(self):
        if self.busy:
            messagebox.showinfo("Busy", "Wait for the current operation to finish.")
            return

        # Rebuilding replaces every row, discarding what the inventory
        # found. A later phase then runs with no record of which switches
        # answered, so this is worth a prompt rather than a surprise.
        collected = sum(1 for r in self.rows.values() if r.state.stack_members)
        if collected and not messagebox.askyesno(
            "Discard inventory results?",
            f"Rebuilding the list clears the inventory collected from "
            f"{collected} switch(es).\n\nRebuild anyway?",
        ):
            return

        for row in self.rows.values():
            for widget in row.widgets:
                widget.destroy()
        self.rows.clear()
        self.inventory_ran = False

        ips = self._parse_ips(report_errors=True)
        if not ips:
            messagebox.showwarning("No IPs", "Enter at least one switch IP.")
            return

        for i, ip in enumerate(ips, start=1):
            self.rows[ip] = SwitchRow(self.table, ip, i, self._reload_single)

        self._log(f"Built list of {len(ips)} switch(es).")
        self._update_summary()

    def _set_all_selected(self, value):
        for row in self.rows.values():
            row.selected.set(value)

    def _apply_row_filter(self):
        """
        Hides rows for addresses that never answered. A /24 scan is mostly
        empty addresses, and hiding them leaves just the switches on screen.
        Hidden rows keep their state - unticking the box brings them back.
        """
        hide = self.var_hide_unreachable.get()
        for row in self.rows.values():
            unreachable = row.state.status == engine.UNREACHABLE
            row.set_visible(not (hide and unreachable))
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    # --------------------------------------------------------
    # Config assembly
    # --------------------------------------------------------

    def _build_config(self, require_upgrade_fields=False):
        """
        Assembles an UpgradeConfig from the panel.

        Only the upgrade path needs a TFTP server and image definitions,
        so require_upgrade_fields is set for prepare and left off for the
        read-only phases - inventory and file collection run on
        credentials alone.
        """
        if not self.var_user.get().strip():
            messagebox.showwarning("Missing username", "Enter a username.")
            return None
        if not self.var_pass.get():
            messagebox.showwarning("Missing password", "Enter a password.")
            return None
        if require_upgrade_fields and not self.var_tftp.get().strip():
            messagebox.showwarning("Missing TFTP server", "Enter the TFTP server IP.")
            return None

        families = {}
        for entry in self.image_rows:
            variables = entry["vars"]
            prefix = variables["prefix"].get().strip()
            image = variables["image"].get().strip()
            # A row with neither a prefix nor an image is an empty slot,
            # not a mistake - it is simply unused.
            if not prefix and not image:
                continue
            if not prefix or not image:
                messagebox.showwarning(
                    "Incomplete image row",
                    f"{'Image filename' if prefix else 'Prefix'} is missing for "
                    f"'{prefix or image}'.\n\nFill both in, or clear the row with "
                    "the \u2212 button.")
                return None
            if prefix in families:
                messagebox.showwarning(
                    "Duplicate prefix",
                    f"'{prefix}' is listed more than once. Each prefix matches one "
                    "image, so remove the row you are not using.")
                return None
            try:
                size_kb = int(variables["size_kb"].get().strip() or "0")
            except ValueError:
                messagebox.showwarning("Bad size", f"Size for {prefix} must be a number.")
                return None
            families[prefix] = engine.ImageSpec(
                image=image,
                md5=variables["md5"].get().strip(),
                target_version=variables["version"].get().strip(),
                size_kb=size_kb,
            )

        if require_upgrade_fields and not families:
            messagebox.showwarning("No images", "Configure at least one image.")
            return None

        try:
            workers = max(1, int(self.var_workers.get()))
        except ValueError:
            workers = 3
        try:
            scan_workers = max(1, int(self.var_scan_workers.get()))
        except ValueError:
            scan_workers = 20
        try:
            probe_timeout = max(0.1, float(self.var_probe.get()))
        except ValueError:
            probe_timeout = 1.5

        self._workers = workers
        self._scan_workers = scan_workers
        try:
            transfers = max(1, int(self.var_transfers.get()))
        except ValueError:
            transfers = 1
        method = METHOD_BY_LABEL.get(self.var_method.get(), engine.METHOD_BUNDLE)
        return engine.UpgradeConfig(
            upgrade_method=method,
            max_concurrent_transfers=transfers,
            username=self.var_user.get().strip(),
            password=self.var_pass.get(),
            tftp_server=self.var_tftp.get().strip(),
            family_images=families,
            probe_timeout=probe_timeout,
            output_dir=self.var_outdir.get().strip(),
        )

    # --------------------------------------------------------
    # Inventory (read-only)
    # --------------------------------------------------------

    def _start_inventory(self):
        if self.busy:
            return
        if not self.rows:
            messagebox.showwarning("No switches", "Build the switch list first.")
            return

        config = self._build_config()
        if not config:
            return

        targets = [ip for ip, row in self.rows.items() if row.selected.get()]
        if not targets:
            messagebox.showwarning("Nothing selected", "Select at least one switch.")
            return

        self._set_busy(True)
        self.cancel_flag.clear()
        self._log(f"=== INVENTORY START: {len(targets)} address(es), "
                  f"{self._scan_workers} at a time, {config.probe_timeout:g}s probe. "
                  f"Read-only - nothing is changed. ===")

        threading.Thread(target=self._inventory_worker,
                         args=(targets, config), daemon=True).start()

    def _inventory_worker(self, targets, config):
        reporter = engine.Reporter(self._post)
        try:
            # Inventory is read-only and light, so it runs much wider
            # than the prepare phase. Most addresses in a subnet scan are
            # dead and get settled by the TCP probe in milliseconds.
            workers = min(self._scan_workers, max(len(targets), 1))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(engine.collect_inventory, ip, config,
                                       reporter, self.cancel_flag.is_set)
                           for ip in targets]
                for f in futures:
                    f.result()
        except Exception as e:
            self._post(None, {"log_line": f"Inventory error: {e}"})
        finally:
            self._post(None, {"phase_done": "inventory"})

    def _export_inventory(self):
        collected = [row for row in self.rows.values() if row.state.stack_members]
        if not collected:
            messagebox.showinfo(
                "Nothing to export",
                "Run the inventory first - no switch data has been collected yet.",
            )
            return

        default_name = f"switch_inventory_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        path = filedialog.asksaveasfilename(
            title="Save inventory CSV",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return

        fieldnames = ["ip", "hostname", "stack_member", "model",
                      "serial", "version", "mac", "boot_mode"]

        # Preserve the order the switches were listed in
        ordered_ips = self._parse_ips()
        order = {ip: i for i, ip in enumerate(ordered_ips)}

        export_rows = []
        for row in collected:
            for member in row.state.stack_members:
                export_rows.append(member)
        export_rows.sort(key=lambda r: (
            order.get(r["ip"], 0),
            int(r["stack_member"]) if str(r["stack_member"]).isdigit() else 0,
        ))

        try:
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(export_rows)
        except Exception as e:
            messagebox.showerror("Could not write CSV", str(e))
            return

        self._log(f"Exported {len(export_rows)} row(s) to {path}")

        # Quick on-screen breakdown, same as the standalone inventory script
        by_model, by_version, by_mode = {}, {}, {}
        for r in export_rows:
            by_model[r["model"]] = by_model.get(r["model"], 0) + 1
            by_version[r["version"]] = by_version.get(r["version"], 0) + 1
            if r["boot_mode"]:
                by_mode[r["boot_mode"]] = by_mode.get(r["boot_mode"], 0) + 1

        summary = [f"Exported {len(export_rows)} row(s) to:\n{path}\n", "Models:"]
        summary += [f"  {m}: {n}" for m, n in sorted(by_model.items(), key=lambda x: -x[1])]
        summary.append("\nVersions:")
        summary += [f"  {v}: {n}" for v, n in sorted(by_version.items(), key=lambda x: -x[1])]
        if by_mode:
            summary.append("\nBoot mode:")
            summary += [f"  {m}: {n}" for m, n in sorted(by_mode.items(), key=lambda x: -x[1])]

        messagebox.showinfo("Inventory exported", "\n".join(summary))

    # --------------------------------------------------------
    # File collection: show running-config / show tech-support
    # --------------------------------------------------------

    def _choose_output_dir(self):
        path = filedialog.askdirectory(title="Choose where to save configs and tech-support")
        if path:
            self.var_outdir.set(path)

    def _start_collect(self):
        if self.busy:
            return
        if not self.rows:
            messagebox.showwarning("No switches", "Build the switch list first.")
            return

        want_run = self.var_get_run.get()
        want_tech = self.var_get_tech.get()
        if not (want_run or want_tech):
            messagebox.showwarning(
                "Nothing to collect",
                "Tick running-config, tech-support, or both.")
            return

        if not self.var_outdir.get().strip():
            self._choose_output_dir()
            if not self.var_outdir.get().strip():
                return

        config = self._build_config()
        if not config:
            return

        selected = [ip for ip, row in self.rows.items() if row.selected.get()]
        if self.inventory_ran:
            # A scan that found nothing is still a scan: its result is
            # that there is nothing here, not that it should be ignored.
            kept, skipped = self._split_inventoried(selected)
            self._log_skipped(skipped)
        else:
            # No scan yet - collection can still run on its own, and its
            # per-switch probe drops dead addresses quickly.
            kept = selected
        targets = [(ip, self.rows[ip].state.hostname) for ip in kept]
        if not targets:
            messagebox.showwarning(
                "Nothing selected",
                "Select at least one switch the inventory found.")
            return

        if want_tech and not messagebox.askyesno(
            "Confirm collection",
            f"Pull files from {len(targets)} switch(es)?\n\n"
            "show tech-support takes several minutes per switch and produces "
            "large files.\n\nContinue?",
        ):
            return

        self._set_busy(True)
        self.cancel_flag.clear()
        wanted = " + ".join(
            n for n, on in (("running-config", want_run), ("tech-support", want_tech)) if on)
        self._log(f"=== COLLECT START: {wanted} from {len(targets)} switch(es) "
                  f"-> {config.output_dir} ===")

        threading.Thread(
            target=self._collect_worker,
            args=(targets, config, want_run, want_tech), daemon=True).start()

    def _collect_worker(self, targets, config, want_run, want_tech):
        reporter = engine.Reporter(self._post)
        results = []
        try:
            workers = min(self._scan_workers, max(len(targets), 1))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(engine.collect_files, ip, config, reporter,
                                want_run, want_tech,
                                self.cancel_flag.is_set, hostname)
                    for ip, hostname in targets
                ]
                for f in futures:
                    results.append(f.result())
        except Exception as e:
            self._post(None, {"log_line": f"Collection error: {e}"})
        finally:
            self._write_skipped_summary(config, results)
            self._post(None, {"phase_done": "collect"})

    def _write_skipped_summary(self, config, results):
        """Mirrors the skipped_switches.txt the standalone export script wrote."""
        saved = sum(1 for r in results if r.get("ok"))
        skipped = [r for r in results if not r.get("ok")]
        self._post(None, {"log_line": f"Collected from {saved} switch(es), "
                                      f"{len(skipped)} skipped."})
        if not skipped or not config.output_dir:
            return
        path = os.path.join(config.output_dir, "skipped_switches.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"Skipped switches ({len(skipped)} total)\n")
                f.write("=" * 40 + "\n\n")
                for r in skipped:
                    f.write(f"{r['ip']}: {r.get('error', 'unknown')}\n")
            self._post(None, {"log_line": f"Skipped list written to {path}"})
        except Exception as e:
            self._post(None, {"log_line": f"Could not write skipped list: {e}"})

    # --------------------------------------------------------
    # Phase 1: prepare
    # --------------------------------------------------------

    def _start_prepare(self):
        if self.busy:
            return
        if not self.rows:
            messagebox.showwarning("No switches", "Build the switch list first.")
            return

        config = self._build_config(require_upgrade_fields=True)
        if not config:
            return

        targets = [ip for ip, row in self.rows.items() if row.selected.get()]
        if not targets:
            messagebox.showwarning("Nothing selected", "Select at least one switch.")
            return

        # Only switches the inventory positively found are queued. A
        # subnet scan leaves hundreds of empty addresses behind, and
        # every one of them would otherwise be dialled here.
        if not self.inventory_ran:
            messagebox.showwarning(
                "Run the inventory first",
                "Prepare only runs against switches the inventory has found.\n\n"
                "Run Inventory (read-only) first - it takes seconds and confirms "
                "which addresses actually have a switch behind them.")
            return

        targets, skipped = self._split_inventoried(targets)
        self._log_skipped(skipped)
        if not targets:
            messagebox.showinfo(
                "Nothing to prepare",
                f"The inventory did not find a switch at any of the "
                f"{len(skipped)} selected address(es).\n\n"
                "Re-run the inventory to pick up anything that has come back.")
            return

        if config.upgrade_method != engine.METHOD_BUNDLE:
            if not messagebox.askyesno(
                "Install workflow selected",
                f"Upgrading via: {self.var_method.get()}\n\n"
                "This uses install add / activate / commit instead of setting a "
                "boot system variable. It has not been validated against hardware "
                "the way the bundle path has.\n\n"
                "Try it on one switch before running it across a site.\n\n"
                "Continue?",
            ):
                return

        unverified = [p for p, s in config.family_images.items()
                      if not s.md5 or s.md5.upper().startswith("PUT_")]
        if unverified:
            proceed = messagebox.askyesno(
                "MD5 not configured",
                f"No expected MD5 for: {', '.join(unverified)}.\n\n"
                "Transfers for those models will not be verified, so a silently "
                "truncated image could be installed.\n\nContinue anyway?",
            )
            if not proceed:
                return

        self._set_busy(True)
        self.cancel_flag.clear()
        self._log(f"=== PREPARE START: {len(targets)} switch(es), "
                  f"{self._workers} at a time. Nothing reloads in this phase. ===")

        thread = threading.Thread(
            target=self._prepare_worker, args=(targets, config), daemon=True)
        thread.start()

    def _prepare_worker(self, targets, config):
        reporter = engine.Reporter(self._post)
        try:
            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                futures = [
                    pool.submit(engine.prepare_switch, ip, config, reporter,
                                self.cancel_flag.is_set)
                    for ip in targets
                ]
                for f in futures:
                    f.result()
        except Exception as e:
            self._post(None, {"log_line": f"Prepare phase error: {e}"})
        finally:
            self._post(None, {"phase_done": "prepare"})

    # --------------------------------------------------------
    # Phase 2: reload
    # --------------------------------------------------------

    def _reload_single(self, row):
        """
        Reloads one switch on its own thread.

        This deliberately does not mark the window busy. Reloading a
        switch takes many minutes, and an operator working through a
        stack of them needs to start the next one whenever they are
        ready rather than waiting for the previous switch to come back.
        Only the row that was started is locked; every other row stays
        live.
        """
        if self.busy or row.ip in self._reloading:
            return
        config = self._build_config()
        if not config:
            return
        if not messagebox.askyesno(
            "Confirm reload",
            f"Reload {row.ip} ({row.state.hostname})?\n\nThis switch will go down.",
        ):
            return

        self._reloading.add(row.ip)
        self._refresh_controls()
        self._log(f"=== RELOAD: {row.ip} ({row.state.hostname}) "
                  f"({len(self._reloading)} reload(s) in flight) ===")
        threading.Thread(target=self._single_reload_worker,
                         args=(row.state, config), daemon=True).start()

    def _single_reload_worker(self, state, config):
        reporter = engine.Reporter(self._post)
        try:
            engine.reload_switch(state, config, reporter)
        except Exception as e:
            self._post(None, {"log_line": f"Reload error on {state.ip}: {e}"})
        finally:
            # Reports only this switch, so the others keep running.
            self._post(None, {"reload_done": state.ip})

    def _start_reloads(self, parallel):
        if self.busy or self._reloading:
            return
        config = self._build_config()
        if not config:
            return

        ready = [row.state for ip, row in self.rows.items()
                 if row.selected.get() and row.state.status == engine.PREPARED]

        if not ready:
            messagebox.showinfo(
                "Nothing ready",
                "No selected switch has finished preparing with a verified boot variable.",
            )
            return

        names = "\n".join(f"  {s.ip} ({s.hostname})" for s in ready)
        mode = "ALL AT THE SAME TIME" if parallel else "one at a time"
        if not messagebox.askyesno(
            "Confirm reload",
            f"Reload these {len(ready)} switch(es) {mode}?\n\n{names}\n\n"
            + ("Every one of them will be down simultaneously."
               if parallel else "Each will be reloaded and verified before the next."),
        ):
            return

        self._set_busy(True)
        self._log(f"=== RELOAD START: {len(ready)} switch(es), {mode} ===")
        threading.Thread(target=self._reload_worker,
                         args=(ready, config, parallel), daemon=True).start()

    def _reload_worker(self, states, config, parallel):
        reporter = engine.Reporter(self._post)
        try:
            if parallel:
                with ThreadPoolExecutor(max_workers=len(states)) as pool:
                    futures = [pool.submit(engine.reload_switch, s, config, reporter)
                               for s in states]
                    for f in futures:
                        f.result()
            else:
                for s in states:
                    engine.reload_switch(s, config, reporter)
        except Exception as e:
            self._post(None, {"log_line": f"Reload phase error: {e}"})
        finally:
            self._post(None, {"phase_done": "reload"})

    # --------------------------------------------------------
    # Queue plumbing (worker threads -> UI thread)
    # --------------------------------------------------------

    def _post(self, ip, fields):
        """Called from worker threads. Only touches the queue, never widgets."""
        self.msg_queue.put((ip, fields))

    def _drain_queue(self):
        try:
            while True:
                ip, fields = self.msg_queue.get_nowait()

                if ip is None:
                    if "log_line" in fields:
                        self._log(fields["log_line"])
                    if "reload_done" in fields:
                        # One switch finished. The others keep running, so
                        # only this row is released.
                        self._reloading.discard(fields["reload_done"])
                        self._refresh_controls()
                        self._update_summary()
                        if not self._reloading:
                            self._log("=== ALL SINGLE RELOADS COMPLETE ===")
                    if "phase_done" in fields:
                        self._set_busy(False)
                        self._apply_row_filter()
                        self._update_summary()
                        self._log(f"=== {fields['phase_done'].upper()} PHASE COMPLETE: "
                                  f"{self._status_tally()} ===")
                        self._log_non_responders()
                        if fields["phase_done"] == "inventory":
                            self.inventory_ran = True
                    continue

                row = self.rows.get(ip)
                if not row:
                    continue

                if "log_append" in fields:
                    self._log(f"{ip}: {fields['log_append']}")
                    row.apply(fields)
                    continue

                row.apply(fields)

                if "status" in fields:
                    # A subnet scan is mostly dead addresses, and every
                    # address reports "probing" then "connecting". Logging
                    # all of that would bury the switches that answered,
                    # so only settled states reach the log - the status
                    # column and the summary line carry the rest.
                    if fields["status"] not in (engine.UNREACHABLE, engine.INVENTORYING):
                        self._log(f"{ip} ({row.state.hostname}): {fields['status']}"
                                  + (f" - {fields.get('message')}" if fields.get("message") else ""))
                    self._update_summary()
        except queue.Empty:
            pass

        self.root.after(100, self._drain_queue)

    # --------------------------------------------------------
    # Misc UI helpers
    # --------------------------------------------------------

    def _log(self, text):
        self.txt_log.insert("end", text + "\n")
        self.txt_log.see("end")

    def _set_busy(self, busy):
        self.busy = busy
        self._refresh_controls()

    def _refresh_controls(self):
        """
        Sets every button from the current state.

        Two things can be running: a batch phase, which owns the window,
        and any number of single-switch reloads, which own only their own
        row. A phase-wide control is disabled while either is happening;
        a row's Reload button is disabled only while that row is busy.
        """
        anything_running = self.busy or bool(self._reloading)
        state = "disabled" if anything_running else "normal"
        self.btn_inventory.config(state=state)
        self.btn_collect.config(state=state)
        self.btn_prepare.config(state=state)
        self.btn_reload_selected.config(state=state)
        self.btn_reload_all.config(state=state)

        # Export reads collected inventory and writes a local file, so
        # reloads in flight are no reason to withhold it.
        has_data = any(r.state.stack_members for r in self.rows.values())
        self.btn_export.config(
            state="normal" if (has_data and not self.busy) else "disabled")
        self.btn_cancel.config(state="normal" if anything_running else "disabled")

        for ip, row in self.rows.items():
            row.set_button_enabled(not self.busy and ip not in self._reloading)

    def _cancel(self):
        self.cancel_flag.set()
        self._log("Cancel requested - switches already mid-step will finish that step first.")

    def _split_inventoried(self, ips):
        """
        Splits addresses into those an inventory actually reached and
        those it did not.

        This is an allowlist on purpose. Listing the addresses to avoid
        cannot work for a subnet scan: of 254 addresses, ten are
        switches and the rest are nothing at all, arriving in whatever
        state the scan left them - never scanned, scan cancelled, or
        something that accepted a connection but was not a switch. Only
        a switch the inventory positively found is worth a later phase.

        Returns (found, not_found).
        """
        found, not_found = [], []
        for ip in ips:
            row = self.rows.get(ip)
            if row is not None and row.state.inventoried:
                found.append(ip)
            else:
                not_found.append(ip)
        return found, not_found

    def _log_skipped(self, skipped):
        if not skipped:
            return
        shown = ", ".join(skipped[:12])
        if len(skipped) > 12:
            shown += f", ...and {len(skipped) - 12} more"
        self._log(f"Skipping {len(skipped)} address(es) the inventory did not "
                  f"find a switch at: {shown}")

    def _log_non_responders(self):
        """
        Names the addresses that did not answer.

        Individual non-responses are kept out of the log so a subnet scan
        stays readable, but they must not be invisible either - one of
        them is exactly the switch that will fail a later phase.
        """
        quiet = [ip for ip, row in self.rows.items()
                 if row.state.status == engine.UNREACHABLE]
        if not quiet:
            return
        shown = ", ".join(quiet[:12])
        if len(quiet) > 12:
            shown += f", ...and {len(quiet) - 12} more"
        self._log(f"No answer from {len(quiet)} address(es): {shown}")

    def _status_tally(self):
        counts = {}
        for row in self.rows.values():
            counts[row.state.status] = counts.get(row.state.status, 0) + 1
        return ", ".join(f"{n} {status.lower()}"
                         for status, n in sorted(counts.items(), key=lambda x: -x[1]))

    def _update_summary(self):
        counts = {}
        for row in self.rows.values():
            counts[row.state.status] = counts.get(row.state.status, 0) + 1
        parts = [f"{status}: {n}" for status, n in counts.items()]
        self.lbl_summary.config(text="   ".join(parts))


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    UpgradeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
