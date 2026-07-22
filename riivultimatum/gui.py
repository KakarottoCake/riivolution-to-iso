"""A small Tkinter front-end over `pipeline.build`.

Design goals, in order: do the same thing the CLI does (no second code path
for the actual work), stack multiple mods with an explicit apply-order, and
stay a single window a first-time user can read top to bottom.

Threading rule: the build runs on a worker thread and touches no widgets. It
pushes log lines and a final result onto a queue; the UI thread drains that
queue on a timer. Tkinter is not thread-safe, and this is the whole reason the
window stays responsive during a multi-minute extract/compose.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, pipeline
from .disc.wit import find_wit
from .pipeline import BuildRequest, ModSpec, PipelineError
from .riivo.parser import Disc, RiivolutionXmlError


@dataclass
class GuiMod:
    """A mod in the list, with its parsed options and the user's choices."""

    xml: Path
    disc: Disc
    sd_root: Path | None = None
    #: "Section/Option" -> chosen choice name, or "" for disabled.
    selections: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.xml.stem

    def to_spec(self) -> ModSpec:
        # An empty selections dict with all_defaults=False would be rejected for
        # a mod that has options, so signal "the user has decided" explicitly.
        return ModSpec(
            xml=self.xml,
            sd_root=self.sd_root,
            selections=dict(self.selections),
            all_defaults=True,
        )

    def summary(self) -> str:
        active = [f"{k.split('/')[-1]}={v}" for k, v in self.selections.items() if v]
        if not active:
            return "(no options enabled)"
        return ", ".join(active)


# A queue message is either ("log", str) or ("done", BuildResult|Exception).
_Message = tuple


class App(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=10)
        master.title(f"Riivolution Ultimatum {__version__}")
        master.minsize(820, 660)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.mods: list[GuiMod] = []
        self._queue: queue.Queue[_Message] = queue.Queue()
        self._building = False

        self._build_widgets()
        self._refresh_wit_status()

    # -- layout ----------------------------------------------------------

    def _build_widgets(self) -> None:
        row = 0

        # Source ISO.
        iso_frame = ttk.LabelFrame(self, text="1. Source game", padding=8)
        iso_frame.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        iso_frame.columnconfigure(1, weight=1)
        ttk.Label(iso_frame, text="ISO / WBFS:").grid(row=0, column=0, sticky="w")
        self.iso_var = tk.StringVar()
        ttk.Entry(iso_frame, textvariable=self.iso_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(iso_frame, text="Browse...", command=self._pick_iso).grid(row=0, column=2)
        self.game_label = ttk.Label(iso_frame, text="", foreground="#555")
        self.game_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.iso_var.trace_add("write", lambda *_: self._refresh_game_label())
        row += 1

        # Mods: list on the left, the selected mod's options on the right.
        mods_frame = ttk.LabelFrame(self, text="2. Mods to apply (stacked top to bottom)", padding=8)
        mods_frame.grid(row=row, column=0, sticky="nsew", pady=(0, 8))
        self.rowconfigure(row, weight=1)
        mods_frame.columnconfigure(1, weight=1)
        mods_frame.rowconfigure(0, weight=1)

        left = ttk.Frame(mods_frame)
        left.grid(row=0, column=0, sticky="ns")
        self.mod_list = tk.Listbox(left, height=8, exportselection=False, width=28)
        self.mod_list.grid(row=0, column=0, rowspan=6, sticky="ns")
        self.mod_list.bind("<<ListboxSelect>>", lambda _e: self._show_options())
        ttk.Button(left, text="Add...", width=9, command=self._add_mod).grid(row=0, column=1, padx=4)
        ttk.Button(left, text="Remove", width=9, command=self._remove_mod).grid(row=1, column=1, padx=4)
        ttk.Button(left, text="Up", width=9, command=lambda: self._move(-1)).grid(row=2, column=1, padx=4)
        ttk.Button(left, text="Down", width=9, command=lambda: self._move(1)).grid(row=3, column=1, padx=4)
        ttk.Label(left, text="Later mods win\non conflicts.", foreground="#777",
                  justify="left").grid(row=4, column=1, padx=4, pady=(8, 0), sticky="n")

        # Scrollable options panel for the selected mod.
        opt_outer = ttk.Frame(mods_frame)
        opt_outer.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        opt_outer.columnconfigure(0, weight=1)
        opt_outer.rowconfigure(0, weight=1)
        self.opt_canvas = tk.Canvas(opt_outer, highlightthickness=0)
        self.opt_canvas.grid(row=0, column=0, sticky="nsew")
        opt_scroll = ttk.Scrollbar(opt_outer, orient="vertical", command=self.opt_canvas.yview)
        opt_scroll.grid(row=0, column=1, sticky="ns")
        self.opt_canvas.configure(yscrollcommand=opt_scroll.set)
        self.opt_inner = ttk.Frame(self.opt_canvas)
        self._opt_window = self.opt_canvas.create_window((0, 0), window=self.opt_inner, anchor="nw")
        self.opt_inner.bind(
            "<Configure>",
            lambda _e: self.opt_canvas.configure(scrollregion=self.opt_canvas.bbox("all")),
        )
        self.opt_canvas.bind(
            "<Configure>", lambda e: self.opt_canvas.itemconfigure(self._opt_window, width=e.width)
        )
        self._options_placeholder()
        row += 1

        # Output settings.
        out_frame = ttk.LabelFrame(self, text="3. Output", padding=8)
        out_frame.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        out_frame.columnconfigure(1, weight=1)

        self.wbfs_var = tk.BooleanVar(value=False)
        fmt = ttk.Frame(out_frame)
        fmt.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(fmt, text="ISO file", variable=self.wbfs_var, value=False,
                        command=self._refresh_out_hint).grid(row=0, column=0)
        ttk.Radiobutton(fmt, text="WBFS folder on a USB drive (USB Loader GX layout)",
                        variable=self.wbfs_var, value=True,
                        command=self._refresh_out_hint).grid(row=0, column=1, padx=(10, 0))

        ttk.Label(out_frame, text="Destination:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.out_var = tk.StringVar()
        ttk.Entry(out_frame, textvariable=self.out_var).grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(out_frame, text="Browse...", command=self._pick_out).grid(row=1, column=2, pady=(6, 0))
        self.out_hint = ttk.Label(out_frame, text="", foreground="#777")
        self.out_hint.grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 0))

        adv = ttk.Frame(out_frame)
        adv.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.keep_id_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            adv,
            text="Keep original game ID (default: change the game code so the mod gets its own save slot)",
            variable=self.keep_id_var,
        ).grid(row=0, column=0, sticky="w")
        self._refresh_out_hint()
        row += 1

        # Actions + wit status.
        action = ttk.Frame(self)
        action.grid(row=row, column=0, sticky="ew", pady=(0, 6))
        action.columnconfigure(2, weight=1)
        self.build_btn = ttk.Button(action, text="Build", command=lambda: self._start(dry_run=False))
        self.build_btn.grid(row=0, column=0)
        self.dry_btn = ttk.Button(action, text="Dry run", command=lambda: self._start(dry_run=True))
        self.dry_btn.grid(row=0, column=1, padx=6)
        self.wit_label = ttk.Label(action, text="", foreground="#777")
        self.wit_label.grid(row=0, column=2, sticky="e")
        row += 1

        # Log.
        log_frame = ttk.LabelFrame(self, text="Log", padding=6)
        log_frame.grid(row=row, column=0, sticky="nsew")
        self.rowconfigure(row, weight=2)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=10, wrap="word", state="disabled",
                           font=("Consolas", 9), background="#111", foreground="#ddd")
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)

    # -- ISO -------------------------------------------------------------

    def _pick_iso(self) -> None:
        path = filedialog.askopenfilename(
            title="Select the original game image",
            filetypes=[("Wii disc images", "*.iso *.wbfs"), ("All files", "*.*")],
        )
        if path:
            self.iso_var.set(path)

    def _refresh_game_label(self) -> None:
        iso = self.iso_var.get().strip()
        if not iso or not Path(iso).is_file():
            self.game_label.configure(text="")
            return
        try:
            boot = pipeline.read_boot(Path(iso))
        except PipelineError as exc:
            self.game_label.configure(text=str(exc), foreground="#b00")
            return
        self.game_label.configure(
            text=f"{boot.game_id}   {boot.title}   (rev {boot.revision})", foreground="#282"
        )

    # -- mod list --------------------------------------------------------

    def _add_mod(self) -> None:
        path = filedialog.askopenfilename(
            title="Select a Riivolution XML",
            filetypes=[("Riivolution XML", "*.xml"), ("All files", "*.*")],
        )
        if not path:
            return
        xml = Path(path)
        try:
            disc = pipeline.load_disc(ModSpec(xml=xml))
        except (PipelineError, RiivolutionXmlError) as exc:
            messagebox.showerror("Could not read XML", str(exc))
            return

        mod = GuiMod(xml=xml, disc=disc)
        # Seed selections from each option's XML default.
        for section in disc.sections:
            for option in section.options:
                key = f"{section.name}/{option.name}"
                if 1 <= option.selected_choice <= len(option.choices):
                    mod.selections[key] = option.choices[option.selected_choice - 1].name
                else:
                    mod.selections[key] = ""
        self.mods.append(mod)
        self._refresh_mod_list()
        self.mod_list.selection_clear(0, tk.END)
        self.mod_list.selection_set(tk.END)
        self._show_options()

    def _remove_mod(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        del self.mods[index]
        self._refresh_mod_list()
        self._show_options()

    def _move(self, delta: int) -> None:
        index = self._selected_index()
        if index is None:
            return
        target = index + delta
        if not (0 <= target < len(self.mods)):
            return
        self.mods[index], self.mods[target] = self.mods[target], self.mods[index]
        self._refresh_mod_list()
        self.mod_list.selection_set(target)
        self._show_options()

    def _selected_index(self) -> int | None:
        sel = self.mod_list.curselection()
        return sel[0] if sel else None

    def _refresh_mod_list(self) -> None:
        keep = self._selected_index()
        self.mod_list.delete(0, tk.END)
        for i, mod in enumerate(self.mods, start=1):
            self.mod_list.insert(tk.END, f"{i}. {mod.name}")
        if keep is not None and keep < len(self.mods):
            self.mod_list.selection_set(keep)

    # -- options panel ---------------------------------------------------

    def _options_placeholder(self, text: str = "Add a mod, then select it to choose its options.") -> None:
        for child in self.opt_inner.winfo_children():
            child.destroy()
        ttk.Label(self.opt_inner, text=text, foreground="#777").grid(row=0, column=0, sticky="w", padx=4, pady=4)

    def _show_options(self) -> None:
        index = self._selected_index()
        if index is None:
            self._options_placeholder()
            return
        mod = self.mods[index]
        for child in self.opt_inner.winfo_children():
            child.destroy()

        if not mod.disc.sections:
            ttk.Label(
                self.opt_inner,
                text=f"{mod.name} has no options; all its patches apply unconditionally.",
                foreground="#777", wraplength=440, justify="left",
            ).grid(row=0, column=0, sticky="w", padx=4, pady=4)
            return

        r = 0
        for section in mod.disc.sections:
            ttk.Label(self.opt_inner, text=section.name, font=("", 9, "bold")).grid(
                row=r, column=0, columnspan=2, sticky="w", padx=4, pady=(6, 2)
            )
            r += 1
            for option in section.options:
                key = f"{section.name}/{option.name}"
                ttk.Label(self.opt_inner, text=option.name).grid(row=r, column=0, sticky="w", padx=(16, 6))
                values = ["(disabled)"] + [c.name for c in option.choices]
                var = tk.StringVar(value=mod.selections.get(key) or "(disabled)")
                combo = ttk.Combobox(self.opt_inner, values=values, textvariable=var, state="readonly", width=32)
                combo.grid(row=r, column=1, sticky="w", pady=1)
                combo.bind(
                    "<<ComboboxSelected>>",
                    lambda _e, m=mod, k=key, v=var: self._set_choice(m, k, v.get()),
                )
                r += 1
        self.opt_inner.columnconfigure(1, weight=1)

    def _set_choice(self, mod: GuiMod, key: str, value: str) -> None:
        mod.selections[key] = "" if value == "(disabled)" else value
        # Reflect the new summary in the list without losing selection.
        self._refresh_mod_list()

    # -- output ----------------------------------------------------------

    def _refresh_out_hint(self) -> None:
        if self.wbfs_var.get():
            self.out_hint.configure(
                text="Pick the drive's wbfs/ folder; the game is written as "
                "'<Title> [<ID>]/<ID>.wbfs', split at 4 GB for FAT32."
            )
        else:
            self.out_hint.configure(text="Pick where to save the patched .iso file.")

    def _pick_out(self) -> None:
        if self.wbfs_var.get():
            path = filedialog.askdirectory(title="Select the drive's wbfs/ folder")
        else:
            path = filedialog.asksaveasfilename(
                title="Save patched ISO as", defaultextension=".iso",
                filetypes=[("Wii ISO", "*.iso")],
            )
        if path:
            self.out_var.set(path)

    # -- build -----------------------------------------------------------

    def _refresh_wit_status(self) -> None:
        wit = find_wit()
        if wit:
            self.wit_label.configure(text=f"wit: {Path(wit).name} ✓", foreground="#282")
        else:
            self.wit_label.configure(
                text="wit not found — install Wiimms ISO Tools to build", foreground="#b00"
            )

    def _start(self, *, dry_run: bool) -> None:
        if self._building:
            return
        try:
            request = self._make_request(dry_run=dry_run)
        except PipelineError as exc:
            messagebox.showerror("Cannot start", str(exc))
            return

        self._building = True
        self.build_btn.state(["disabled"])
        self.dry_btn.state(["disabled"])
        self._clear_log()
        self._append_log(("Dry run" if dry_run else "Build") + " started...\n")

        threading.Thread(target=self._worker, args=(request,), daemon=True).start()
        self.after(80, self._drain_queue)

    def _make_request(self, *, dry_run: bool) -> BuildRequest:
        iso = self.iso_var.get().strip()
        if not iso:
            raise PipelineError("choose a source ISO first")
        if not self.mods:
            raise PipelineError("add at least one mod")
        out = self.out_var.get().strip()
        if not dry_run and not out:
            raise PipelineError("choose an output destination")

        return BuildRequest(
            iso=Path(iso),
            mods=[m.to_spec() for m in self.mods],
            out=Path(out) if out else None,
            wbfs=self.wbfs_var.get(),
            keep_game_id=self.keep_id_var.get(),
            dry_run=dry_run,
        )

    def _worker(self, request: BuildRequest) -> None:
        try:
            result = pipeline.build(request, progress=lambda m: self._queue.put(("log", m + "\n")))
            self._queue.put(("done", result))
        except Exception as exc:  # surfaced to the user on the UI thread
            self._queue.put(("done", exc))

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                else:
                    self._finish(payload)
                    return
        except queue.Empty:
            pass
        if self._building:
            self.after(80, self._drain_queue)

    def _finish(self, payload: object) -> None:
        self._building = False
        self.build_btn.state(["!disabled"])
        self.dry_btn.state(["!disabled"])

        if isinstance(payload, PipelineError):
            self._append_log(f"\nError: {payload}\n")
            messagebox.showerror("Build failed", str(payload))
            return
        if isinstance(payload, Exception):
            self._append_log(f"\nUnexpected error: {payload}\n")
            messagebox.showerror("Build failed", f"{type(payload).__name__}: {payload}")
            return

        result = payload  # BuildResult
        self._append_log("\n" + result.report.render() + "\n")
        if not result.ok:
            messagebox.showwarning(
                "Some patches failed",
                "The build stopped because some patches failed. See the log.",
            )
            return
        if result.output:
            msg = f"Done: {result.output}"
            if self.wbfs_var.get():
                msg += (
                    "\n\nIn USB Loader GX, set this game's IOS to your cIOS slot "
                    "(usually 249) and turn Ocarina/cheats OFF — the mod's code "
                    "is already baked in."
                )
            self._append_log(f"\n{msg}\n")
            messagebox.showinfo("Build complete", msg)
        else:
            self._append_log("\nDry run complete; no image written.\n")

    # -- log -------------------------------------------------------------

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(tk.END, text)
        self.log.see(tk.END)
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", tk.END)
        self.log.configure(state="disabled")


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"error: could not open a display for the GUI: {exc}")
        return 1
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
