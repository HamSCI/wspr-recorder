"""Policy 2026-10-23: no per-channel AGC.  The `usb` preset defaults to
agc=yes, so every channel request must carry agc_enable=0 and gain 0 dB,
whatever the config says."""

from types import SimpleNamespace
from unittest import mock

from wspr_recorder import receiver_manager as rm
from wspr_recorder.config import ChannelDefaults


def test_config_cannot_turn_agc_on_or_set_gain():
    d = ChannelDefaults.from_dict({"agc": True, "gain": 12.0})
    assert d.agc is False
    assert d.gain == 0.0


def test_provision_sends_agc_off_and_zero_gain_even_if_config_says_otherwise():
    mgr = rm.ReceiverManager.__new__(rm.ReceiverManager)
    defaults = ChannelDefaults()
    defaults.agc, defaults.gain = True, 12.0          # bypass the loader clamp
    mgr.config = SimpleNamespace(
        channel_defaults=defaults,
        processing=SimpleNamespace(radiod_lifetime_frames=0))
    mgr._control = mock.Mock()
    mgr._control.ensure_channel.return_value = SimpleNamespace(
        ssrc=5, multicast_address="239.1.1.1", port=5004)
    mgr._input_samprate_hz = 64_800_000
    mgr.state = SimpleNamespace(channels={})
    mgr._status_listener = None
    mgr.sink_factory = mock.Mock()
    mgr._multi_by_group = {}
    mgr._lifetime_entries = []
    multi = mock.MagicMock()
    multi.prune_frequency.return_value = []
    with mock.patch.object(rm, "MultiStream", return_value=multi):
        mgr._provision(14_095_600)
    ens = mgr._control.ensure_channel.call_args.kwargs
    add = multi.add_channel.call_args.kwargs
    for kw in (ens, add):
        assert kw["agc_enable"] == 0
        assert kw["gain"] == 0.0
