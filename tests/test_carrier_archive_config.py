"""[carrier_archive] in config.toml: parsed, validated, off by default."""
from pathlib import Path

import pytest

from wspr_recorder.config import (
    CarrierArchiveConfig, Config, DEFAULT_CARRIER_ARCHIVE_ROOT, load_config,
)

BASE = """
[radiod]
status = "test-status.local"

[[band]]
frequency = "14095600"
modes = ["W2"]
"""


def _load(tmp_path: Path, extra: str = "") -> Config:
    p = tmp_path / "config.toml"
    p.write_text(BASE + extra)
    return load_config(str(p))


def test_absent_section_is_disabled_with_defaults(tmp_path):
    ca = _load(tmp_path).carrier_archive
    assert ca.enabled is False
    assert ca.root == DEFAULT_CARRIER_ARCHIVE_ROOT
    assert (ca.max_age_days, ca.max_disk_pct) == (3, 90)


def test_default_root_is_on_disk_not_tmpfs():
    # The WAV output_dir is /dev/shm (RAM); a 3-day archive must not live there.
    assert DEFAULT_CARRIER_ARCHIVE_ROOT == "/var/lib/wspr-recorder/carrier-archive"
    assert not DEFAULT_CARRIER_ARCHIVE_ROOT.startswith("/dev/shm")


def test_section_parsed(tmp_path):
    ca = _load(tmp_path, """
[carrier_archive]
enabled = true
root = "/srv/carriers"
max_age_days = 7
max_disk_pct = 80
""").carrier_archive
    assert ca == CarrierArchiveConfig(enabled=True, root="/srv/carriers",
                                      max_age_days=7, max_disk_pct=80)


def test_partial_section_keeps_defaults(tmp_path):
    ca = _load(tmp_path, "[carrier_archive]\nenabled = true\n").carrier_archive
    assert ca.enabled is True
    assert ca.root == DEFAULT_CARRIER_ARCHIVE_ROOT
    assert (ca.max_age_days, ca.max_disk_pct) == (3, 90)


@pytest.mark.parametrize("body, needle", [
    ('enabled = "yes"', "enabled"),
    ('root = "relative/dir"', "root"),
    ('root = ""', "root"),
    ("max_age_days = 0", "max_age_days"),
    ("max_age_days = 1.5", "max_age_days"),
    ("max_disk_pct = 0", "max_disk_pct"),
    ("max_disk_pct = 101", "max_disk_pct"),
    ('max_disk_pct = "90"', "max_disk_pct"),
])
def test_invalid_values_rejected(tmp_path, body, needle):
    with pytest.raises(ValueError, match=needle):
        _load(tmp_path, f"[carrier_archive]\n{body}\n")


def test_validated_even_when_disabled(tmp_path):
    """A typo is reported now, not the day someone flips enabled."""
    with pytest.raises(ValueError, match="max_age_days"):
        _load(tmp_path, "[carrier_archive]\nenabled = false\nmax_age_days = -1\n")


def test_example_config_documents_section_commented_out():
    text = (Path(__file__).parent.parent / "config.toml.example").read_text()
    assert "# [carrier_archive]" in text
    assert "\n[carrier_archive]" not in text          # off unless uncommented
