"""This client's copy of the reporter-id rules, run against the shared
table, and the one place the daemon uses it (sigmond
tasks/spec-reporter-id-v372.md §5, W1).

tests/data/reporter_id_vectors.json must stay a byte-identical copy of
sigmond's tests/data/reporter_id_vectors.json; sigmond's
tests/test_reporter_id_drift.py fails when they differ.  Refresh it with:

    cp ../sigmond/tests/data/reporter_id_vectors.json tests/data/

Both modules load by file path or under a stand-in package:
wspr_recorder/__init__.py imports ka9q-python and hamsci-dsp, which a
test venv may lack (the same reason tests/test_configurator.py does this).
"""
import importlib
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "wspr_recorder"

_spec = importlib.util.spec_from_file_location(
    "_wspr_reporter_id_under_test", PKG / "_reporter_id.py")
rid = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rid)

VECTORS = json.loads(
    (REPO / "tests" / "data" / "reporter_id_vectors.json").read_text())


class VectorTableTests(unittest.TestCase):

    def test_reporter_ids(self):
        for v in VECTORS["reporter_ids"]:
            with self.subTest(input=v["input"]):
                self.assertEqual(rid.to_storage(v["input"]), v["storage"])
                self.assertEqual(rid.systemd_instance(v["storage"]), v["systemd"])
                self.assertEqual(rid.unescape_systemd_instance(v["systemd"]), v["storage"])
                self.assertEqual(rid.display(v["storage"]), v["display"])
                self.assertEqual(rid.display(v["systemd"]), v["display"])

    def test_unit_names_escape_only_what_systemd_must(self):
        for v in VECTORS["escape_only"]:
            with self.subTest(name=v["name"]):
                self.assertEqual(rid.systemd_instance(v["name"]), v["systemd"])

    def test_invalid_text_is_refused(self):
        for text in VECTORS["invalid"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                rid.to_storage(text)

    def test_display_never_shows_an_escape(self):
        for v in VECTORS["display_only"]:
            with self.subTest(input=v["input"]):
                self.assertEqual(rid.display(v["input"]), v["display"])


class DaemonInstanceTests(unittest.TestCase):
    """W1: _handle_daemon hands the legacy entry point the storage form."""

    def _daemon_argv(self, instance):
        pkg = types.ModuleType("wspr_recorder")
        pkg.__path__ = [str(PKG)]
        seen = {}
        legacy = types.ModuleType("wspr_recorder.__main__")

        def main():
            seen["argv"] = list(sys.argv)
            return 0
        legacy.main = main
        pkg.__main__ = legacy
        with mock.patch.dict(sys.modules, {"wspr_recorder": pkg,
                                           "wspr_recorder.__main__": legacy}), \
             mock.patch.object(sys, "argv", ["wspr-recorder"]):
            sys.modules.pop("wspr_recorder.cli", None)
            sys.modules.pop("wspr_recorder._reporter_id", None)
            cli = importlib.import_module("wspr_recorder.cli")
            with mock.patch.object(cli, "_install_sighup_handler", lambda: None), \
                 self.assertRaises(SystemExit):
                cli._handle_daemon(SimpleNamespace(config=None, instance=instance,
                                                   memprofile=False))
        return seen["argv"]

    def test_systemd_spelling_becomes_storage(self):
        self.assertEqual(self._daemon_argv("AC0G\\x3dB4")[-2:], ["--instance", "AC0G=B4"])

    def test_every_escape_is_decoded_not_only_x3d(self):
        self.assertEqual(self._daemon_argv("W1ABC\\x2d5")[-2:], ["--instance", "W1ABC-5"])


if __name__ == "__main__":
    unittest.main()
