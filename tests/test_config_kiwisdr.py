"""Tests for kiwisdr [[source]] config (Phase 4c)."""

import os
import tempfile

from unittest import mock

from wspr_recorder.config import Config, load_config


def load_config_from_text(text):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "wspr-recorder.toml")
        with open(p, "w") as f:
            f.write(text)
        # load_config raises on validation errors; these tests call
        # validate() themselves, so parse with the load-time check off.
        with mock.patch.object(Config, "validate", return_value=[]):
            return load_config(p)


def kiwi_cfg(channels, band_freqs, modes=("W2",)):
    out = f'[[source]]\nkiwisdr = "10.20.23.73:8073"\nchannels = {channels}\n'
    for fr in band_freqs:
        ms = ", ".join(f'"{m}"' for m in modes)
        out += f"[[band]]\nfrequency = {fr}\nmodes = [{ms}]\n"
    return out


def test_kiwisdr_source_parses():
    cfg = load_config_from_text('''
[[source]]
kiwisdr = "10.20.23.73:8073"
channels = 8
[[band]]
frequency = 14095600
modes = ["W2"]
''')
    s = cfg.sources[0]
    assert (s.kind, s.host, s.http_port, s.channels) == ("kiwisdr", "10.20.23.73", 8073, 8)
    assert s.key == "kiwisdr:10.20.23.73:8073"
    assert s.password == ""
    assert s.status_address == ""


def test_kiwisdr_default_port_8073():
    s = load_config_from_text('[[source]]\nkiwisdr = "kiwi.local"\nchannels = 4\n').sources[0]
    assert s.http_port == 8073


def test_kiwisdr_more_bands_than_channels_is_error():
    cfg = load_config_from_text(kiwi_cfg(channels=2, band_freqs=[7038600, 10138700, 14095600]))
    assert any("3 bands > 2 channels" in e for e in cfg.validate())


def test_kiwisdr_non_wspr_mode_is_error():
    cfg = load_config_from_text(kiwi_cfg(channels=4, band_freqs=[14074000], modes=["FT8"]))
    assert any("WSPR only" in e for e in cfg.validate())


def test_kiwisdr_missing_channels_is_error():
    cfg = load_config_from_text('[[source]]\nkiwisdr = "kiwi.local"\n')
    assert any("kiwisdr source needs channels" in e for e in cfg.validate())


def test_kiwisdr_valid_config_has_no_kiwi_errors():
    cfg = load_config_from_text(kiwi_cfg(channels=4, band_freqs=[14095600]))
    assert not [e for e in cfg.validate() if "kiwisdr" in e]
    assert not [e for e in cfg.validate() if "status_address" in e or "port" in e]


def test_radiod_source_unchanged():
    s = load_config_from_text('[[source]]\nstatus = "hf.local"\n').sources[0]
    assert (s.kind, s.key) == ("radiod", "radiod:hf.local")
