"""Tests for nixspec_logic (run anywhere) and nixspec_app (need Textual and
nixspec.py beside this file; skipped otherwise).

    python -m unittest -v test_nixspec_ext
"""

import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import nixspec_logic as L

DEFAULT = Path("/home/u/.var/app/org.vinegarhq.Sober/config/sober/config.json")


class CliTests(unittest.TestCase):
    def test_default_env_and_flag_priority(self):
        a = L.parse_cli([], {}, DEFAULT)
        self.assertEqual((a.config, a.source), (DEFAULT, "default"))
        a = L.parse_cli([], {"NIXSPEC_CONFIG": "/e/c.json"}, DEFAULT)
        self.assertEqual((str(a.config), a.source), ("/e/c.json", "$NIXSPEC_CONFIG"))
        a = L.parse_cli(["--config", "/f/c.json"], {"NIXSPEC_CONFIG": "/e"}, DEFAULT)
        self.assertEqual((str(a.config), a.source), ("/f/c.json", "--config"))
        a = L.parse_cli(["--config=/g/c.json"], {}, DEFAULT)
        self.assertEqual(str(a.config), "/g/c.json")

    def test_empty_env_is_ignored(self):
        self.assertEqual(L.parse_cli([], {"NIXSPEC_CONFIG": "  "}, DEFAULT).source, "default")

    def test_missing_value_is_an_error_not_a_silent_default(self):
        for argv in (["--config"], ["--config", ""], ["--config", "--show-path"], ["--config="]):
            with self.assertRaises(L.CliError, msg=argv):
                L.parse_cli(argv, {}, DEFAULT)

    def test_unknown_argument_rejected_and_flags_parsed(self):
        with self.assertRaises(L.CliError):
            L.parse_cli(["--bogus"], {}, DEFAULT)
        a = L.parse_cli(["--show-path", "--version", "-h"], {}, DEFAULT)
        self.assertTrue(a.show_path and a.show_version and a.show_help)

    def test_tilde_expanded(self):
        self.assertNotIn("~", str(L.parse_cli(["--config", "~/c.json"], {}, DEFAULT).config))


BASE = {"DFIntTaskSchedulerTargetFps": "240", "FFlagA": True,
        "FIntB": 10, "FStringC": "hello", "FIntBig": 2, "FFlagZ": False}


def names(rows):
    return [r.name for r in rows]


class ViewTests(unittest.TestCase):
    def test_no_filters_shows_everything_sorted_by_name(self):
        rows = L.view_rows(BASE, BASE)
        self.assertEqual(names(rows), sorted(BASE, key=str.lower))
        self.assertTrue(all(r.status == "same" for r in rows))

    def test_search_name_value_case_insensitive_and_terms(self):
        self.assertEqual(names(L.view_rows(BASE, BASE, query="fps")), ["DFIntTaskSchedulerTargetFps"])
        self.assertEqual(names(L.view_rows(BASE, BASE, query="240")), ["DFIntTaskSchedulerTargetFps"])
        self.assertEqual(names(L.view_rows(BASE, BASE, query="HELLO")), ["FStringC"])
        self.assertEqual(sorted(names(L.view_rows(BASE, BASE, query="true"))), ["FFlagA"])
        self.assertEqual(names(L.view_rows(BASE, BASE, query="fint 10")), ["FIntB"])  # AND terms
        self.assertEqual(L.view_rows(BASE, BASE, query="nothing-matches"), [])

    def test_bool_search_does_not_match_string_false_by_accident(self):
        flags = {"A": False, "B": "x"}
        self.assertEqual(names(L.view_rows(flags, flags, query="false")), ["A"])

    def test_type_filter_distinguishes_strings_numbers_booleans(self):
        self.assertEqual(sorted(names(L.view_rows(BASE, BASE, type_filter="boolean"))), ["FFlagA", "FFlagZ"])
        self.assertEqual(sorted(names(L.view_rows(BASE, BASE, type_filter="number"))), ["FIntB", "FIntBig"])
        self.assertEqual(sorted(names(L.view_rows(BASE, BASE, type_filter="string"))),
                         ["DFIntTaskSchedulerTargetFps", "FStringC"])

    def test_string_240_is_not_a_number(self):
        flags = {"X": "240", "Y": 240}
        self.assertEqual(names(L.view_rows(flags, flags, type_filter="number")), ["Y"])

    def test_sort_modes(self):
        flags = {"b": 2, "a": 10, "c": True, "d": "zeta", "e": "alpha", "f": 2}
        self.assertEqual(names(L.view_rows(flags, flags, sort="name-desc")), ["f", "e", "d", "c", "b", "a"])
        asc = names(L.view_rows(flags, flags, sort="value"))
        self.assertEqual(asc, ["c", "b", "f", "a", "e", "d"])  # bool, numbers (ties by name), strings
        desc = names(L.view_rows(flags, flags, sort="value-desc"))
        self.assertEqual(desc, ["d", "e", "a", "b", "f", "c"])  # ties keep name order

    def test_huge_integers_do_not_crash_sorting(self):
        flags = {"a": 10 ** 400, "b": -(10 ** 400), "c": 1}
        self.assertEqual(names(L.view_rows(flags, flags, sort="value")), ["b", "c", "a"])

    def test_invalid_filter_or_sort_rejected(self):
        with self.assertRaises(ValueError):
            L.view_rows(BASE, BASE, type_filter="nope")
        with self.assertRaises(ValueError):
            L.view_rows(BASE, BASE, sort="nope")

    def test_modified_only_lists_added_changed_and_removed(self):
        staged = dict(BASE)
        staged["New"] = "1"            # added
        staged["FIntB"] = 11           # changed
        del staged["FFlagZ"]           # removed
        rows = {r.name: r for r in L.view_rows(staged, BASE, modified_only=True)}
        self.assertEqual({n: r.status for n, r in rows.items()},
                         {"New": "added", "FIntB": "changed", "FFlagZ": "removed"})
        self.assertIs(rows["FFlagZ"].value, False)   # shows the value on disk
        self.assertNotIn("FFlagZ", names(L.view_rows(staged, BASE)))  # hidden normally
        self.assertEqual(names(L.view_rows(staged, BASE, modified_only=True, query="new")), ["New"])
        self.assertEqual(names(L.view_rows(staged, BASE, modified_only=True, type_filter="boolean")), ["FFlagZ"])

    def test_type_change_counts_as_changed(self):
        self.assertEqual(L.flag_status("X", {"X": 1}, {"X": True}), "changed")
        self.assertEqual(L.flag_status("X", {"X": "2"}, {"X": 2}), "changed")

    def test_unknown_flags_are_never_dropped_from_view(self):
        flags = {"CompletelyUnknownThing": "x", "WeirdName": 3}
        self.assertEqual(len(L.view_rows(flags, flags)), 2)

    def test_view_does_not_mutate_inputs(self):
        staged, base = dict(BASE, New="1"), dict(BASE)
        before = (dict(staged), dict(base))
        L.view_rows(staged, base, query="a", modified_only=True, sort="value")
        self.assertEqual((staged, base), before)


class ResetTests(unittest.TestCase):
    def test_reverts_changed_flag(self):
        staged = dict(BASE, FIntB=99)
        new, action = L.reset_flag(staged, BASE, "FIntB")
        self.assertEqual((action, new["FIntB"]), ("reverted", 10))
        self.assertEqual(staged["FIntB"], 99)          # input untouched

    def test_discards_added_flag(self):
        new, action = L.reset_flag(dict(BASE, New="1"), BASE, "New")
        self.assertEqual(action, "discarded")
        self.assertEqual(new, BASE)

    def test_restores_removed_flag_with_original_type(self):
        staged = {k: v for k, v in BASE.items() if k != "FFlagZ"}
        new, action = L.reset_flag(staged, BASE, "FFlagZ")
        self.assertEqual(action, "restored")
        self.assertIs(new["FFlagZ"], False)

    def test_unchanged_and_unknown(self):
        self.assertEqual(L.reset_flag(BASE, BASE, "FIntB")[1], "unchanged")
        self.assertEqual(L.reset_flag(BASE, BASE, "Nope")[1], "unknown")

    def test_string_vs_number_edit_is_reverted(self):
        new, action = L.reset_flag({"X": 240}, {"X": "240"}, "X")
        self.assertEqual((action, new["X"]), ("reverted", "240"))
        self.assertIsInstance(new["X"], str)


class PruneTests(unittest.TestCase):
    def test_prune_broken_keeps_newest_only(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text("{}")
            for i in range(5):
                (Path(d) / f"config.json.broken-2026010{i}-000000-000000.bak").write_text("x")
            keepme = Path(d) / "config.json.backup-1.bak"
            keepme.write_text("{}")
            removed = L.prune_broken(cfg, keep=2)
            left = sorted(p.name for p in Path(d).glob("config.json.broken-*"))
            self.assertEqual(len(removed), 3)
            self.assertEqual(left, ["config.json.broken-20260103-000000-000000.bak",
                                    "config.json.broken-20260104-000000-000000.bak"])
            self.assertTrue(keepme.exists() and cfg.exists())


def ok(rc=0):
    return mock.Mock(returncode=rc)


class LaunchTests(unittest.TestCase):
    def test_flatpak_missing(self):
        r = L.launch_sober(which=lambda _: None)
        self.assertFalse(r.ok)
        self.assertIn("Flatpak", r.message)

    def test_sober_not_installed(self):
        r = L.launch_sober(run=lambda *a, **k: ok(1), which=lambda _: "/usr/bin/flatpak")
        self.assertFalse(r.ok)
        self.assertIn("flatpak install flathub org.vinegarhq.Sober", r.message)

    def test_info_timeout_and_oserror(self):
        def timeout(*a, **k):
            raise subprocess.TimeoutExpired("flatpak", 10)
        self.assertIn("timed out", L.sober_install_problem(timeout, lambda _: "/f"))

        def boom(*a, **k):
            raise PermissionError("denied")
        self.assertIn("denied", L.sober_install_problem(boom, lambda _: "/f"))

    def test_launch_uses_argument_list_without_shell(self):
        proc = mock.Mock(pid=42)
        proc.wait.side_effect = [subprocess.TimeoutExpired("x", 1), 0]
        popen = mock.Mock(return_value=proc)
        r = L.launch_sober(run=lambda *a, **k: ok(), popen=popen,
                           which=lambda _: "/usr/bin/flatpak", check_seconds=0.01)
        self.assertTrue(r.ok)
        self.assertEqual(r.pid, 42)
        self.assertEqual(popen.call_args[0][0], ["/usr/bin/flatpak", "run", "org.vinegarhq.Sober"])
        kw = popen.call_args[1]
        self.assertNotIn("shell", kw)
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)

    def test_immediate_failure_reports_exit_code_and_stderr(self):
        def fake_popen(argv, stderr, **kw):
            stderr.write(b"error: no display")
            stderr.flush()
            proc = mock.Mock()
            proc.wait.return_value = 3
            return proc
        r = L.launch_sober(run=lambda *a, **k: ok(), popen=fake_popen, which=lambda _: "/f")
        self.assertFalse(r.ok)
        self.assertIn("code 3", r.message)
        self.assertIn("no display", r.message)

    def test_popen_oserror(self):
        r = L.launch_sober(run=lambda *a, **k: ok(),
                           popen=mock.Mock(side_effect=PermissionError("no")),
                           which=lambda _: "/f")
        self.assertFalse(r.ok)
        self.assertIn("Could not start", r.message)

    def test_guard_blocks_repeats_until_cooldown(self):
        now = [100.0]
        g = L.LaunchGuard(3.0, clock=lambda: now[0])
        self.assertTrue(g.try_begin())
        self.assertFalse(g.try_begin())      # still launching
        g.finish()
        now[0] += 1
        self.assertFalse(g.try_begin())      # cooldown
        now[0] += 3
        self.assertTrue(g.try_begin())


# ------------------------------------------------------------------ app tests
try:
    import textual  # noqa: F401
    import nixspec
    import nixspec_app
    HAVE_APP = True
except ImportError:  # pragma: no cover
    HAVE_APP = False

CONFIG_DATA = {"enable_gamemode": True, "use_opengl": False,
               "fflags": {"DFIntTaskSchedulerTargetFps": "240", "FFlagA": True,
                          "FIntB": 10, "FStringC": "hello"}}


@unittest.skipUnless(HAVE_APP, "needs textual and nixspec.py beside the tests")
class AppTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.cfg = self.dir / "config.json"
        self.cfg.write_text(json.dumps(CONFIG_DATA, indent=4))
        for p in (mock.patch.object(nixspec, "CONFIG", self.cfg),
                  mock.patch.object(nixspec, "SETTINGS_PATH", self.dir / "ui.json")):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    async def ready(pilot):
        await pilot.pause()
        await pilot.pause()   # extensions are mounted after the first refresh

    def test_filters_search_sort_and_empty_state(self):
        async def go():
            app = nixspec_app.NixspecPlus()
            async with app.run_test(size=(140, 45)) as pilot:
                await self.ready(pilot)
                table = app.query_one("#flags")
                self.assertEqual(table.row_count, 4)
                app.query_one("#search").value = "hello"
                await pilot.pause()
                self.assertEqual(table.row_count, 1)
                app.query_one("#search").value = "zzz"
                await pilot.pause()
                self.assertEqual(table.row_count, 0)
                self.assertIn("No flags match", str(app.query_one("#ff-summary").render()))
                app.query_one("#search").value = ""
                await pilot.click("#f-type")          # all -> string
                await pilot.pause()
                self.assertEqual(table.row_count, 2)
                await pilot.click("#f-sort")          # name -> name-desc
                await pilot.pause()
                self.assertEqual(app.sort_mode, "name-desc")
        asyncio.run(go())

    def test_reset_modified_and_restore_removed(self):
        async def go():
            app = nixspec_app.NixspecPlus()
            async with app.run_test(size=(140, 45)) as pilot:
                await self.ready(pilot)
                app.query_one("#flag-name").value = "FIntB"
                app.query_one("#flag-value").value = "99"
                await pilot.click("#upsert")
                self.assertEqual(app.staged["FIntB"], 99)
                await pilot.click("#f-reset")
                self.assertEqual(app.staged["FIntB"], 10)
                app.query_one("#flag-name").value = "FFlagA"
                await pilot.click("#remove")
                await pilot.click("#remove")
                self.assertNotIn("FFlagA", app.staged)
                await pilot.click("#f-mod")           # modified only
                await pilot.pause()
                self.assertEqual(app.query_one("#flags").row_count, 1)
                app.query_one("#flag-name").value = "FFlagA"
                await pilot.press("f8")
                self.assertIs(app.staged["FFlagA"], True)
                self.assertEqual(self.cfg.read_text(), json.dumps(CONFIG_DATA, indent=4))  # nothing written
        asyncio.run(go())

    def test_reset_is_blocked_when_config_is_unreadable(self):
        self.cfg.write_bytes(b"")

        async def go():
            app = nixspec_app.NixspecPlus()
            async with app.run_test(size=(140, 45)) as pilot:
                await self.ready(pilot)
                app.staged["X"] = "1"
                app.query_one("#flag-name").value = "X"
                app.action_reset_flag()
                self.assertIn("X", app.staged)
        asyncio.run(go())

    def test_launch_button_runs_once_and_reports(self):
        calls = []

        def fake_launch():
            calls.append(1)
            return L.LaunchResult(True, "Sober was launched.", 1)

        async def go():
            app = nixspec_app.NixspecPlus()
            with mock.patch.object(L, "launch_sober", fake_launch):
                async with app.run_test(size=(140, 45)) as pilot:
                    await self.ready(pilot)
                    await pilot.click("#launch-sober")
                    await pilot.click("#launch-sober")
                    await pilot.press("f7")
                    for _ in range(20):
                        await pilot.pause(0.05)
                    self.assertEqual(len(calls), 1)
                    self.assertIn("LAUNCHED", str(app.query_one("#statusbar").render()))
        asyncio.run(go())

    def test_launch_failure_is_reported_and_editor_survives(self):
        async def go():
            app = nixspec_app.NixspecPlus()
            fail = lambda: L.LaunchResult(False, "Sober (org.vinegarhq.Sober) is not installed.")
            with mock.patch.object(L, "launch_sober", fail):
                async with app.run_test(size=(140, 45)) as pilot:
                    await self.ready(pilot)
                    await pilot.press("f7")
                    for _ in range(20):
                        await pilot.pause(0.05)
                    self.assertIn("LAUNCH FAILED", str(app.query_one("#statusbar").render()))
                    self.assertEqual(app.query_one("#flags").row_count, 4)
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
