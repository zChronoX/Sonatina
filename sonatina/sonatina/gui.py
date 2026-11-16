"""Tkinter configuration UI: devices, codec matrix, telemetry, log."""

from __future__ import annotations

import tkinter as tk
from dataclasses import replace
from tkinter import ttk

from . import CODECS, devices
from .reg import Profile, Store
from .sbc import SbcParams


class App:
    def __init__(self, root: tk.Tk, store: Store) -> None:
        self.root = root
        self.store = store
        self.devices = []
        self.selected: str | None = None
        self.root.title("Sonatina - configuration")
        self.root.geometry("1000x640")
        self.root.minsize(860, 560)
        self._build()
        self.refresh()
        self._tick()

    # -- layout -------------------------------------------------------------

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)

        left = ttk.LabelFrame(outer, text="Devices", padding=6)
        left.grid(row=0, column=0, sticky="nsew")
        self.device_list = tk.Listbox(left, height=16, exportselection=False)
        self.device_list.pack(fill="both", expand=True)
        self.device_list.bind("<<ListboxSelect>>", self._on_select)
        buttons = ttk.Frame(left)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(buttons, text="Add demo", command=self.add_demo).pack(
            side="left", padx=4
        )

        right = ttk.LabelFrame(outer, text="Codec settings", padding=6)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        outer.columnconfigure(0, weight=1)
        outer.columnconfigure(1, weight=2)
        outer.rowconfigure(0, weight=1)

        self.codec_var = tk.StringVar(value="sbc")
        codec_row = ttk.Frame(right)
        codec_row.pack(fill="x")
        ttk.Label(codec_row, text="Codec:").pack(side="left")
        for name in CODECS:
            ttk.Radiobutton(
                codec_row,
                text=name,
                value=name,
                variable=self.codec_var,
                command=self._update_estimate,
            ).pack(side="left", padx=2)

        self.sbc_frame = ttk.LabelFrame(right, text="SBC", padding=6)
        self.sbc_frame.pack(fill="x", pady=6)
        self.sbc_sampling = tk.StringVar(value="48000")
        self.sbc_mode = tk.StringVar(value="joint")
        self.sbc_blocks = tk.StringVar(value="16")
        self.sbc_subbands = tk.StringVar(value="8")
        self.sbc_alloc = tk.StringVar(value="loudness")
        self.sbc_min = tk.StringVar(value="2")
        self.sbc_max = tk.StringVar(value="53")
        self._combo_row(
            self.sbc_frame,
            0,
            "Sampling",
            self.sbc_sampling,
            ("16000", "32000", "44100", "48000"),
        )
        self._combo_row(
            self.sbc_frame,
            1,
            "Channel mode",
            self.sbc_mode,
            ("mono", "dual", "stereo", "joint"),
        )
        self._combo_row(
            self.sbc_frame, 2, "Blocks", self.sbc_blocks, ("4", "8", "12", "16")
        )
        self._combo_row(self.sbc_frame, 3, "Subbands", self.sbc_subbands, ("4", "8"))
        self._combo_row(
            self.sbc_frame, 4, "Allocation", self.sbc_alloc, ("snr", "loudness")
        )
        row = ttk.Frame(self.sbc_frame)
        row.grid(row=5, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(row, text="Bitpool min/max:").pack(side="left")
        ttk.Spinbox(row, from_=2, to=63, width=5, textvariable=self.sbc_min).pack(
            side="left", padx=2
        )
        ttk.Spinbox(row, from_=2, to=250, width=5, textvariable=self.sbc_max).pack(
            side="left"
        )

        self.aac_frame = ttk.LabelFrame(right, text="AAC", padding=6)
        self.aac_frame.pack(fill="x", pady=6)
        self.aac_sampling = tk.StringVar(value="48000")
        self.aac_channels = tk.StringVar(value="stereo")
        self.aac_bitrate = tk.StringVar(value="256")
        self._combo_row(
            self.aac_frame, 0, "Sampling", self.aac_sampling, ("44100", "48000")
        )
        self._combo_row(
            self.aac_frame, 1, "Channels", self.aac_channels, ("mono", "stereo")
        )
        row = ttk.Frame(self.aac_frame)
        row.grid(row=2, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(row, text="Bitrate kbit/s:").pack(side="left")
        ttk.Spinbox(
            row, from_=64, to=512, increment=32, width=6, textvariable=self.aac_bitrate
        ).pack(side="left", padx=2)

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="Apply", command=self.apply).pack(side="left")
        ttk.Button(actions, text="Bump trigger", command=self.bump).pack(
            side="left", padx=6
        )
        self.estimate_var = tk.StringVar(value="")
        ttk.Label(actions, textvariable=self.estimate_var).pack(side="left", padx=10)

        bottom = ttk.Frame(outer)
        bottom.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        outer.rowconfigure(1, weight=1)
        telemetry_frame = ttk.LabelFrame(bottom, text="Telemetry (Current)", padding=6)
        telemetry_frame.pack(side="left", fill="both", expand=True)
        self.telemetry = tk.Text(telemetry_frame, height=9, width=46, state="disabled")
        self.telemetry.pack(fill="both", expand=True)
        log_frame = ttk.LabelFrame(bottom, text="Log", padding=6)
        log_frame.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.log_text = tk.Text(log_frame, height=9, state="disabled")
        self.log_text.pack(fill="both", expand=True)

    @staticmethod
    def _combo_row(
        parent, row: int, label: str, variable: tk.StringVar, values
    ) -> None:
        ttk.Label(parent, text=label + ":").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Combobox(
            parent, textvariable=variable, values=values, width=12, state="readonly"
        ).grid(row=row, column=1, sticky="w", pady=2)

    # -- helpers ------------------------------------------------------------

    def log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def refresh(self) -> None:
        found = {device.address: device for device in devices.enumerate_sinks()}
        for address in self.store.list_addresses():
            found.setdefault(
                address, devices.Device(address=address, name="(profile only)")
            )
        self.devices = [found[address] for address in sorted(found)]
        self.device_list.delete(0, "end")
        for device in self.devices:
            self.device_list.insert("end", device.summary())
        if not self.devices:
            self.device_list.insert("end", "(no Bluetooth audio device found - use Add demo)")
        self.log("refreshed: %d device(s)" % len(self.devices))

    def add_demo(self) -> None:
        self.store.ensure()
        profile = Profile(device="001122AABBCC", codec="sbc")
        self.store.write_profile(profile)
        self.log("demo profile written for 001122AABBCC")
        self.refresh()

    def _on_select(self, _event=None) -> None:
        selection = self.device_list.curselection()
        if not selection or not self.devices:
            return
        index = selection[0]
        if index >= len(self.devices):
            return
        device = self.devices[index]
        self.selected = device.address
        profile = self.store.read_profile(device.address) or Profile(
            device=device.address
        )
        self._load(profile)
        self._show_current()

    def _load(self, profile: Profile) -> None:
        sbc = profile.sbc
        self.codec_var.set(profile.codec)
        self.sbc_sampling.set(str(sbc.sampling))
        self.sbc_mode.set(sbc.channel_mode)
        self.sbc_blocks.set(str(sbc.blocks))
        self.sbc_subbands.set(str(sbc.subbands))
        self.sbc_alloc.set(sbc.allocation)
        self.sbc_min.set(str(sbc.min_bitpool))
        self.sbc_max.set(str(sbc.max_bitpool))
        self.aac_sampling.set(str(profile.aac_sampling))
        self.aac_channels.set("stereo" if profile.aac_channel_mode else "mono")
        self.aac_bitrate.set(str(profile.aac_bitrate))
        self._update_estimate()

    def _form_sbc(self) -> SbcParams:
        return SbcParams(
            sampling=int(self.sbc_sampling.get()),
            channel_mode=self.sbc_mode.get(),
            blocks=int(self.sbc_blocks.get()),
            subbands=int(self.sbc_subbands.get()),
            allocation=self.sbc_alloc.get(),
            min_bitpool=int(self.sbc_min.get()),
            max_bitpool=int(self.sbc_max.get()),
        )

    def _form_profile(self, device: str) -> Profile:
        return Profile(
            device=device,
            codec=self.codec_var.get(),
            sbc=self._form_sbc(),
            aac_channel_mode=1 if self.aac_channels.get() == "stereo" else 0,
            aac_sampling=int(self.aac_sampling.get()),
            aac_bitrate=int(self.aac_bitrate.get()),
        )

    def _update_estimate(self) -> None:
        try:
            sbc = self._form_sbc()
            sbc.validate()
            self.estimate_var.set("SBC estimate: %.1f kbit/s" % sbc.bitrate_kbps())
        except (ValueError, TypeError):
            self.estimate_var.set("SBC estimate: (invalid parameters)")

    def apply(self) -> None:
        if not self.selected:
            self.log("select a device first")
            return
        try:
            profile = self._form_profile(self.selected)
            profile.sbc.validate()
        except (ValueError, TypeError) as exc:
            self.log("invalid parameters: %s" % exc)
            return
        self.store.write_profile(profile)
        trigger = self.store.bump_trigger()
        self.log(
            "applied %s (%s), trigger=%d" % (self.selected, profile.summary(), trigger)
        )
        self._update_estimate()

    def bump(self) -> None:
        trigger = self.store.bump_trigger()
        self.log("trigger bumped to %d" % trigger)

    def _show_current(self) -> None:
        current = self.store.read_current(self.selected) if self.selected else None
        self.telemetry.configure(state="normal")
        self.telemetry.delete("1.0", "end")
        if current:
            for name in (
                "Codec",
                "Bitpool",
                "Bitrate",
                "Packets",
                "Errors",
                "TxTimeUs",
                "TxTimeAvgUs",
                "TxTimeMaxUs",
                "TxTimeHighCount",
                "TxTimeCriticalCount",
                "AbrAdjustments",
                "TxFlushTO",
                "DelayMs",
                "Opened",
                "UpdatedAt",
            ):
                if name in current:
                    self.telemetry.insert("end", "%-20s %s\n" % (name, current[name]))
        else:
            self.telemetry.insert("end", "no Current data (run: source ...)\n")
        self.telemetry.configure(state="disabled")

    def _tick(self) -> None:
        self._show_current()
        self.root.after(1000, self._tick)

    # -- self test ----------------------------------------------------------

    def selftest(self) -> None:
        self.store.ensure()
        self.refresh()
        self._update_estimate()
        self.root.update_idletasks()
        self.root.update()
        print(
            "gui selftest ok: devices=%d estimate=%s"
            % (len(self.devices), self.estimate_var.get())
        )


def run(args) -> int:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("error: tkinter is not available in this Python build")
        return 1
    store = Store(args.hive, args.root)
    root = tk.Tk()
    app = App(root, store)
    if args.selftest:
        try:
            app.selftest()
        finally:
            root.destroy()
        return 0
    root.mainloop()
    return 0
