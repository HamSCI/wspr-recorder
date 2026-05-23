"""Targeted tests for the whiptail wizard dispatcher in configurator.py.

These live alongside test_configurator.py but exercise the dispatch
helpers added when porting psk-recorder's whiptail pattern.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "_wspr_configurator_wizard_under_test",
    REPO_ROOT / "wspr_recorder" / "configurator.py",
)
configurator = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(configurator)


def _ns(**kwargs):
    base = dict(non_interactive=False, reconfig=False, config=None)
    base.update(kwargs)
    return SimpleNamespace(**base)


class WizardAvailableTests(unittest.TestCase):
    def test_non_interactive_disables_wizard(self):
        args = _ns(non_interactive=True)
        with mock.patch.object(sys.stdout, "isatty", return_value=True), \
             mock.patch("shutil.which", return_value="/usr/bin/whiptail"), \
             mock.patch.object(configurator, "_wizard_script",
                                return_value=Path("/some/wizard.sh")):
            self.assertFalse(configurator._wizard_available(args))

    def test_no_tty_disables_wizard(self):
        args = _ns()
        with mock.patch.object(sys.stdout, "isatty", return_value=False), \
             mock.patch("shutil.which", return_value="/usr/bin/whiptail"), \
             mock.patch.object(configurator, "_wizard_script",
                                return_value=Path("/some/wizard.sh")):
            self.assertFalse(configurator._wizard_available(args))

    def test_no_whiptail_disables_wizard(self):
        args = _ns()
        with mock.patch.object(sys.stdout, "isatty", return_value=True), \
             mock.patch("shutil.which", return_value=None), \
             mock.patch.object(configurator, "_wizard_script",
                                return_value=Path("/some/wizard.sh")):
            self.assertFalse(configurator._wizard_available(args))

    def test_no_script_disables_wizard(self):
        args = _ns()
        with mock.patch.object(sys.stdout, "isatty", return_value=True), \
             mock.patch("shutil.which", return_value="/usr/bin/whiptail"), \
             mock.patch.object(configurator, "_wizard_script",
                                return_value=None):
            self.assertFalse(configurator._wizard_available(args))

    def test_all_conditions_met_enables_wizard(self):
        args = _ns()
        with mock.patch.object(sys.stdout, "isatty", return_value=True), \
             mock.patch("shutil.which", return_value="/usr/bin/whiptail"), \
             mock.patch.object(configurator, "_wizard_script",
                                return_value=Path("/some/wizard.sh")):
            self.assertTrue(configurator._wizard_available(args))


class ExecWizardTests(unittest.TestCase):
    def test_parses_status_address_from_stdout(self):
        args = _ns()
        fake_proc = SimpleNamespace(
            returncode=0,
            stdout="STATUS_ADDRESS=bee1-status.local\n",
            stderr="",
        )
        with mock.patch.object(configurator, "_wizard_script",
                                return_value=Path("/fake/wizard.sh")), \
             mock.patch("subprocess.run", return_value=fake_proc):
            fields = configurator._exec_wizard(args, Path("/tmp/cfg.toml"))
        self.assertEqual(fields, {"status_address": "bee1-status.local"})

    def test_returns_empty_dict_on_cancel(self):
        # Wizard exits 0 with no stdout when user cancels or uses $EDITOR.
        args = _ns()
        fake_proc = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(configurator, "_wizard_script",
                                return_value=Path("/fake/wizard.sh")), \
             mock.patch("subprocess.run", return_value=fake_proc):
            fields = configurator._exec_wizard(args, Path("/tmp/cfg.toml"))
        self.assertEqual(fields, {})

    def test_returns_none_on_nonzero_exit(self):
        args = _ns()
        fake_proc = SimpleNamespace(returncode=2, stdout="",
                                     stderr="something broke\n")
        with mock.patch.object(configurator, "_wizard_script",
                                return_value=Path("/fake/wizard.sh")), \
             mock.patch("subprocess.run", return_value=fake_proc):
            fields = configurator._exec_wizard(args, Path("/tmp/cfg.toml"))
        self.assertIsNone(fields)

    def test_returns_none_when_script_missing(self):
        args = _ns()
        with mock.patch.object(configurator, "_wizard_script",
                                return_value=None):
            fields = configurator._exec_wizard(args, Path("/tmp/cfg.toml"))
        self.assertIsNone(fields)


class EditDispatchTests(unittest.TestCase):
    def test_edit_uses_wizard_when_available(self):
        body = '[radiod]\nstatus_address = "old.local"\n'
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "cfg.toml"
            target.write_text(body)
            args = _ns(config=target, non_interactive=False)
            with mock.patch.object(configurator, "_wizard_available",
                                    return_value=True), \
                 mock.patch.object(configurator, "_exec_wizard",
                                    return_value={"status_address": "new.local"}):
                rc = configurator.cmd_config_edit(args)
            self.assertEqual(rc, 0)
            self.assertIn('status_address = "new.local"', target.read_text())

    def test_edit_falls_back_when_wizard_returns_none(self):
        # Real wizard error should drop through to legacy prompt.
        body = '[radiod]\nstatus_address = "old.local"\n'
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "cfg.toml"
            target.write_text(body)
            args = _ns(config=target, non_interactive=False)
            with mock.patch.object(configurator, "_wizard_available",
                                    return_value=True), \
                 mock.patch.object(configurator, "_exec_wizard",
                                    return_value=None), \
                 mock.patch.object(configurator, "_prompt",
                                    return_value="fallback.local"):
                rc = configurator.cmd_config_edit(args)
            self.assertEqual(rc, 0)
            self.assertIn('status_address = "fallback.local"', target.read_text())

    def test_edit_wizard_cancel_writes_nothing(self):
        body = '[radiod]\nstatus_address = "old.local"\n'
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "cfg.toml"
            target.write_text(body)
            mtime_before = target.stat().st_mtime_ns
            args = _ns(config=target, non_interactive=False)
            with mock.patch.object(configurator, "_wizard_available",
                                    return_value=True), \
                 mock.patch.object(configurator, "_exec_wizard",
                                    return_value={}):
                rc = configurator.cmd_config_edit(args)
            self.assertEqual(rc, 0)
            self.assertEqual(target.read_text(), body)


if __name__ == "__main__":
    unittest.main()
