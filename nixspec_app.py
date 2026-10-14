#!/usr/bin/env python3
"""Nixspec with the improved FastFlags page and the Launch Sober action.

`NixspecPlus` subclasses the existing `nixspec.Nixspec` and leaves nixspec.py
untouched, so all of its save / backup / validation logic is reused as is.

Textual note: Textual calls the handler of the same name in *every* class of
the MRO (subclass first, then base), so handlers here never call super() for
on_* methods; the base handlers still run afterwards.
"""

from __future__ import annotations

import json
import os
import sys

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Input, Static

import nixspec
import nixspec_logic as logic
from nixspec import AnimButton, Nixspec

TYPE_LABEL = {"all": "Type: all", "string": "Type: string",
              "number": "Type: number", "boolean": "Type: boolean"}
SORT_LABEL = {"name": "Sort: name+", "name-desc": "Sort: name-",
              "value": "Sort: value+", "value-desc": "Sort: value-"}
MARKS = {"added": "+ ", "changed": "~ ", "removed": "- ", "same": "  "}
STYLES = {"added": "green", "changed": "yellow", "removed": "red strike", "same": ""}


def _theme_extra_css():
    out = []
    for name, p in nixspec.THEMES.items():
        s = f"Screen.theme-{name.lower()}"
        out.append(
            f"{s} .launch {{ background: {p['raised']}; color: {p['accent']}; "
            f"border: round {p['accent']}; text-style: bold; }}\n"
            f"{s} .launch:hover {{ background: {p['hover']}; }}\n"
            f"{s} .launch:disabled {{ color: {p['muted']}; border: round {p['border']}; }}\n"
            f"{s} #ff-summary {{ color: {p['muted']}; }}\n"
        )
    return "".join(out)


EXTRA_CSS = (
    "#ff-filters { height: 1; margin-bottom: 0; }\n"
    "#ff-summary { height: 1; }\n"
    ".launch { width: 100%; height: 3; margin: 1 0 0 0; }\n"
) + _theme_extra_css()


def _j(value):
    return json.dumps(value, ensure_ascii=False)


class NixspecPlus(Nixspec):
    CSS = Nixspec.CSS + EXTRA_CSS

    BINDINGS = [
        Binding("f7", "launch_sober", "Launch Sober"),
        Binding("f8", "reset_flag", "Reset flag"),
        Binding("f9", "cycle_sort", "Sort"),
        Binding("slash", "focus_search", "Search"),
    ]

    def __init__(self):
        super().__init__()
        self.type_filter = "all"
        self.sort_mode = "name"
        self.modified_only = False
        self.launch_guard = logic.LaunchGuard()
        self._ext_ready = False

    # ------------------------------------------------------------ mounting
    def on_mount(self):
        # Runs BEFORE Nixspec.on_mount (subclass handler first). Defer our
        # widget setup until the base has finished its own.
        self.call_after_refresh(self._setup_extensions)

    async def _setup_extensions(self):
        if self._ext_ready:
            return
        self._ext_ready = True
        left = self.query_one("#ff-left")
        filters = Horizontal(
            AnimButton("", id="f-type", classes="tool"),
            AnimButton("", id="f-sort", classes="tool"),
            AnimButton("", id="f-mod", classes="tool"),
            id="ff-filters", classes="toolrow",
        )
        await left.mount(filters, Static("", id="ff-summary"), before="#flags")
        await self.query_one("#save").parent.mount(
            AnimButton("Reset flag", id="f-reset", classes="tool")
        )
        await self.query_one("#sidebar").mount(
            AnimButton("▶ Launch Sober", id="launch-sober", classes="launch"),
            after="#nav-settings",
        )
        self.query_one("#search", Input).placeholder = "Search name or value  ( / )"
        self.refresh_filter_controls()
        self.refresh_table()

    def refresh_filter_controls(self):
        try:
            self.query_one("#f-type", Button).label = TYPE_LABEL[self.type_filter]
            self.query_one("#f-sort", Button).label = SORT_LABEL[self.sort_mode]
            mod = self.query_one("#f-mod", Button)
            mod.label = "Changed: ON" if self.modified_only else "Changed: off"
            mod.set_class(self.modified_only, "selected")
        except Exception:
            pass  # not mounted yet

    # --------------------------------------------------------------- table
    def _cursor_name(self, table):
        try:
            if table.row_count == 0:
                return None
            return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        except Exception:
            return None

    def refresh_table(self):
        """Overrides Nixspec.refresh_table (same name, same call sites)."""
        table = self.query_one("#flags", DataTable)
        rows = logic.view_rows(
            self.staged, self.baseline_flags, self.filter_text,
            self.type_filter, self.modified_only, self.sort_mode,
        )
        keep = self._cursor_name(table)
        table.clear(columns=False)
        for row in rows:
            style = STYLES[row.status]
            table.add_row(
                Text(MARKS[row.status] + row.name, style=style),
                Text(_j(row.value), style=style),
                Text(row.type.upper(), style=style),
                key=row.name,
            )
        self._shown = len(rows)
        if keep:
            for index, row in enumerate(rows):
                if row.name == keep:
                    try:
                        table.move_cursor(row=index)
                    except Exception:
                        pass
                    break
        self.refresh_summary(len(rows))
        self.refresh_stats()

    def refresh_summary(self, shown):
        try:
            bar = self.query_one("#ff-summary", Static)
        except Exception:
            return
        total = len(self.staged)
        terms = []
        if self.filter_text.strip():
            terms.append(f'search "{self.filter_text.strip()}"')
        if self.type_filter != "all":
            terms.append(f"type {self.type_filter}")
        if self.modified_only:
            terms.append("changed only")
        if shown == 0 and self.modified_only and not self.filter_text.strip() and self.type_filter == "all":
            msg = "No pending changes."
        elif shown == 0 and terms:
            msg = f"No flags match ({', '.join(terms)}). Clear the search or filters."
        elif total == 0 and not self.baseline_flags:
            msg = "No flags yet. Add one below, or use the bulk editor / Import."
        elif terms:
            msg = f"Showing {shown} of {total} flags - {', '.join(terms)}"
        else:
            msg = f"{total} flags.  /: search   F9: sort   F8: reset selected flag"
        bar.update(Text(msg))

    # ------------------------------------------------------------- filters
    @staticmethod
    def _next(seq, current):
        return seq[(seq.index(current) + 1) % len(seq)]

    def cycle_type(self):
        self.type_filter = self._next(logic.TYPE_FILTERS, self.type_filter)
        self._filters_changed()

    def action_cycle_sort(self):
        self.sort_mode = self._next(logic.SORT_MODES, self.sort_mode)
        self._filters_changed()

    def toggle_modified(self):
        self.modified_only = not self.modified_only
        self._filters_changed()

    def _filters_changed(self):
        self.refresh_filter_controls()
        self.refresh_table()
        self.set_status(
            f"{TYPE_LABEL[self.type_filter]}  |  {SORT_LABEL[self.sort_mode]}  |  "
            f"changed only: {'on' if self.modified_only else 'off'}  "
            f"({self._shown} shown)"
        )

    def action_focus_search(self):
        self.show_page("fastflags", animate=False)
        self.set_ff_view("list")
        self.query_one("#search", Input).focus()

    # --------------------------------------------------------- reset flag
    def _target_flag(self):
        typed = self.query_one("#flag-name", Input).value.strip()
        if typed in self.staged or typed in self.baseline_flags:
            return typed
        return self._cursor_name(self.query_one("#flags", DataTable))

    def action_reset_flag(self):
        """Put one flag back to the value last read from / written to disk."""
        if self.load_problem:
            self.set_status(
                "RESET UNAVAILABLE / the config on disk could not be loaded, "
                "so there is no baseline to reset to.", "error",
            )
            return
        name = self._target_flag()
        if not name:
            self.set_status("RESET / Select a flag first.", "error")
            return
        old = self.staged.get(name)
        new, action = logic.reset_flag(self.staged, self.baseline_flags, name)
        if action == "unknown":
            self.set_status(f"RESET / {name} is not a known flag.", "error")
            return
        if action == "unchanged":
            self.set_status(f"{name} already matches the config on disk.")
            return
        self.staged = new
        self.save_state = None
        self.refresh_table()
        name_box = self.query_one("#flag-name", Input)
        value_box = self.query_one("#flag-value", Input)
        if name in new:
            name_box.value, value_box.value = name, _j(new[name])
        else:
            name_box.value = value_box.value = ""
        if action == "reverted":
            msg = f"Reset {name}: {_j(old)} -> {_j(new[name])} (value on disk)."
        elif action == "discarded":
            msg = f"Discarded new flag {name} (not in the config on disk)."
        else:
            msg = f"Restored {name} = {_j(new[name])}; it will no longer be removed."
        self.set_status(msg + " Not written to disk until you Save.", "ok")

    def on_data_table_row_highlighted(self, event):
        # The base handler fills the form for flags in `staged`; this one
        # covers flags pending removal (listed under "Changed: ON").
        table = self.query_one("#flags", DataTable)
        if event.row_key is None or self.focused is not table:
            return
        name = str(event.row_key.value)
        if name not in self.staged and name in self.baseline_flags:
            self.query_one("#flag-name", Input).value = name
            self.query_one("#flag-value", Input).value = _j(self.baseline_flags[name])
            self.set_status(
                f"{name} is pending removal. Reset flag (F8) restores it; "
                "Save removes it from the file."
            )

    # ---------------------------------------------------------- Launch Sober
    def action_launch_sober(self):
        if not self.launch_guard.try_begin():
            self.set_status("Sober is already launching (or just launched). Please wait a moment.")
            return
        self._set_launch_button(busy=True)
        self.set_status("Checking Sober and launching it...")
        self.run_worker(self._launch_worker, thread=True, name="launch-sober",
                        exit_on_error=False)

    def _launch_worker(self):  # worker thread: no UI access here
        try:
            result = logic.launch_sober()
        except Exception as exc:  # never let the worker take the editor down
            result = logic.LaunchResult(False, f"Unexpected launch error: {exc}")
        try:
            self.call_from_thread(self._launch_done, result)
        except Exception:
            pass  # app already closed

    def _launch_done(self, result):
        self.launch_guard.finish()
        self._set_launch_button(busy=False)
        note = ""
        if result.ok and self.unsaved_summary():
            note = (" Note: Sober uses the config on disk; your unsaved edits "
                    "were not applied.")
        self.set_status(
            ("LAUNCHED / " if result.ok else "LAUNCH FAILED / ") + result.message + note,
            "ok" if result.ok else "error",
        )

    def _set_launch_button(self, busy):
        try:
            button = self.query_one("#launch-sober", Button)
            button.disabled = busy
            button.label = "… Launching" if busy else "▶ Launch Sober"
        except Exception:
            pass

    # -------------------------------------------------------------- buttons
    def on_button_pressed(self, event):
        # Base handler runs afterwards and ignores ids it does not know.
        bid = event.button.id or ""
        if bid == "f-type":
            self.cycle_type()
        elif bid == "f-sort":
            self.action_cycle_sort()
        elif bid == "f-mod":
            self.toggle_modified()
        elif bid == "f-reset":
            self.action_reset_flag()
        elif bid == "launch-sober":
            self.action_launch_sober()

    # ------------------------------------------------------------- restore
    def action_restore_backup(self):
        super().action_restore_backup()
        logic.prune_broken(nixspec.CONFIG)   # *.broken-*.bak grew without limit


# --------------------------------------------------------------------------
# Entry point (`nixspec` command)
# --------------------------------------------------------------------------


def _version():
    try:
        from importlib.metadata import version
        return version("nixspec")
    except Exception:
        return "dev"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        cli = logic.parse_cli(argv, os.environ, nixspec.DEFAULT_CONFIG)
    except logic.CliError as exc:
        print(f"nixspec: {exc}\n\n{logic.USAGE}", file=sys.stderr)
        return 2
    if cli.show_help:
        print(logic.USAGE)
        return 0
    if cli.show_version:
        print(f"nixspec {_version()}")
        return 0
    nixspec.CONFIG, nixspec.CONFIG_SOURCE = cli.config, cli.source
    if cli.show_path:
        print(f"Sober config ({cli.source}): {cli.config}")
        print(f"Exists: {cli.config.exists()}")
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("nixspec is a terminal application; run it in a terminal "
              "(or from the application menu entry).", file=sys.stderr)
        return 1
    NixspecPlus().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
