import pytest

from repeater.config import get_radio_for_board


class _DummyRadio:
    _initialized = True

    def begin(self):
        return True


def test_get_radio_for_board_passes_en_pins(monkeypatch):
    captured_kwargs = {}

    class _DummySX1262Radio(_DummyRadio):
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

    monkeypatch.setattr(
        "openhop_core.hardware.sx1262_wrapper.SX1262Radio",
        _DummySX1262Radio,
    )

    board_config = {
        "radio_type": "sx1262",
        "sx1262": {
            "bus_id": 0,
            "cs_id": 0,
            "cs_pin": -1,
            "reset_pin": 18,
            "busy_pin": 5,
            "irq_pin": 6,
            "txen_pin": -1,
            "rxen_pin": -1,
            "en_pins": [26, 23],
        },
        "radio": {
            "frequency": 915000000,
            "tx_power": 22,
            "spreading_factor": 9,
            "bandwidth": 125000,
            "coding_rate": 5,
            "preamble_length": 17,
            "sync_word": 0x3444,
        },
    }

    get_radio_for_board(board_config)

    assert captured_kwargs["en_pins"] == [26, 23]
    assert "en_pin" not in captured_kwargs


def test_get_radio_for_board_null_radio_type_returns_null_radio():
    radio = get_radio_for_board({"radio_type": None})
    assert type(radio).__name__ == "NullRadio"


def test_get_radio_for_board_missing_radio_type_returns_null_radio():
    radio = get_radio_for_board({})
    assert type(radio).__name__ == "NullRadio"


# ─── modem_tcp / modem_usb branches ────────────────────────────────────


def _modem_radio_cfg():
    """Common radio params for the pymc_* tests."""
    return {
        "frequency": 869618000,
        "tx_power": 22,
        "spreading_factor": 8,
        "bandwidth": 62500,
        "coding_rate": 8,
        "preamble_length": 16,
        "sync_word": 0x12,
    }


def test_get_radio_for_board_modem_tcp(monkeypatch):
    pytest.importorskip("openhop_core.hardware.tcp_radio")
    captured = {}

    class _DummyTCPLoRaRadio(_DummyRadio):
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "openhop_core.hardware.tcp_radio.TCPLoRaRadio",
        _DummyTCPLoRaRadio,
    )

    board_config = {
        "radio_type": "modem_tcp",
        "modem_tcp": {
            "host": "pymc-3e2834.local",
            "port": 5055,
            "token": "shared-secret",
            "connect_timeout": 7.5,
            "lbt_enabled": False,
            "lbt_max_attempts": 3,
        },
        "radio": _modem_radio_cfg(),
    }

    get_radio_for_board(board_config)

    assert captured["host"] == "pymc-3e2834.local"
    assert captured["port"] == 5055
    assert captured["token"] == "shared-secret"
    assert captured["connect_timeout"] == 7.5
    assert captured["frequency"] == 869618000
    assert captured["sync_word"] == 0x12
    assert captured["lbt_enabled"] is False
    assert captured["lbt_max_attempts"] == 3


def test_get_radio_for_board_modem_tcp_requires_host(monkeypatch):
    pytest.importorskip("openhop_core.hardware.tcp_radio")

    monkeypatch.setattr(
        "openhop_core.hardware.tcp_radio.TCPLoRaRadio",
        lambda **kwargs: _DummyRadio(),
    )

    board_config = {
        "radio_type": "modem_tcp",
        "modem_tcp": {"port": 5055},
        "radio": _modem_radio_cfg(),
    }

    with pytest.raises(ValueError, match="Missing 'host'"):
        get_radio_for_board(board_config)


def test_get_radio_for_board_modem_usb(monkeypatch):
    pytest.importorskip("openhop_core.hardware.usb_radio")
    captured = {}

    class _DummyUSBLoRaRadio(_DummyRadio):
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "openhop_core.hardware.usb_radio.USBLoRaRadio",
        _DummyUSBLoRaRadio,
    )

    board_config = {
        "radio_type": "modem_usb",
        "modem_usb": {
            "port": "/dev/ttyACM0",
            "baudrate": 921600,
        },
        "radio": _modem_radio_cfg(),
    }

    get_radio_for_board(board_config)

    assert captured["port"] == "/dev/ttyACM0"
    assert captured["baudrate"] == 921600
    assert captured["frequency"] == 869618000
    assert captured["sync_word"] == 0x12
    # LBT defaults preserved when omitted from modem_usb section.
    assert captured["lbt_enabled"] is True
    assert captured["lbt_max_attempts"] == 5


def test_get_radio_for_board_modem_usb_requires_port(monkeypatch):
    pytest.importorskip("openhop_core.hardware.usb_radio")

    monkeypatch.setattr(
        "openhop_core.hardware.usb_radio.USBLoRaRadio",
        lambda **kwargs: _DummyRadio(),
    )

    board_config = {
        "radio_type": "modem_usb",
        # Section present (baudrate set) but `port` deliberately omitted to
        # exercise the inner "Missing 'port'" guard rather than the outer
        # "Missing 'modem_usb' section" one.
        "modem_usb": {"baudrate": 921600},
        "radio": _modem_radio_cfg(),
    }

    with pytest.raises(ValueError, match="Missing 'port'"):
        get_radio_for_board(board_config)


@pytest.mark.parametrize(
    ("legacy_type", "legacy_section", "module_name", "class_name", "settings"),
    [
        (
            "pymc_tcp",
            "pymc_tcp",
            "openhop_core.hardware.tcp_radio",
            "TCPLoRaRadio",
            {"host": "legacy.local"},
        ),
        (
            "pymc_usb",
            "pymc_usb",
            "openhop_core.hardware.usb_radio",
            "USBLoRaRadio",
            {"port": "/dev/ttyACM0"},
        ),
    ],
)
def test_legacy_modem_aliases_still_construct_radio(
    monkeypatch, legacy_type, legacy_section, module_name, class_name, settings
):
    pytest.importorskip(module_name)
    monkeypatch.setattr(f"{module_name}.{class_name}", lambda **kwargs: _DummyRadio())

    radio = get_radio_for_board(
        {
            "radio_type": legacy_type,
            legacy_section: settings,
            "radio": _modem_radio_cfg(),
        }
    )

    assert type(radio).__name__ == "BaselineCrcCounterRadio"


# ─── kiss branch: optional CSMA / key-up tuning forwarding ────────────


def _kiss_capture_radio_config(monkeypatch):
    """Patch KissModemWrapper to capture the radio_config it is built with."""
    pytest.importorskip("openhop_core.hardware.kiss_modem_wrapper")
    captured = {}

    class _DummyKissWrapper(_DummyRadio):
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

    monkeypatch.setattr(
        "openhop_core.hardware.kiss_modem_wrapper.KissModemWrapper",
        _DummyKissWrapper,
    )
    return captured


def test_get_radio_for_board_kiss_forwards_csma_tuning(monkeypatch):
    captured = _kiss_capture_radio_config(monkeypatch)

    board_config = {
        "radio_type": "kiss",
        "kiss": {
            "port": "/dev/ttyACM0",
            "baud_rate": 115200,
            "kiss_persistence": 255,
            "kiss_slottime_ms": 20,
            "tx_delay_ms": 50,
            "kiss_full_duplex": True,
        },
        "radio": _modem_radio_cfg(),
    }

    get_radio_for_board(board_config)

    rc = captured["kwargs"]["radio_config"]
    assert rc["kiss_persistence"] == 255
    assert rc["kiss_slottime_ms"] == 20
    assert rc["tx_delay_ms"] == 50
    assert rc["kiss_full_duplex"] is True


def test_get_radio_for_board_kiss_omits_unset_tuning(monkeypatch):
    captured = _kiss_capture_radio_config(monkeypatch)

    board_config = {
        "radio_type": "kiss",
        "kiss": {"port": "/dev/ttyACM0", "baud_rate": 115200},
        "radio": _modem_radio_cfg(),
    }

    get_radio_for_board(board_config)

    rc = captured["kwargs"]["radio_config"]
    # Unset keys must not be forwarded, so the wrapper keeps its own defaults.
    for key in (
        "kiss_persistence",
        "kiss_slottime_ms",
        "tx_delay_ms",
        "kiss_full_duplex",
        "agc_reset_interval_seconds",
        "fem_rx_gain",
        "fem_tx_gain",
        "rx_boosted_gain",
    ):
        assert key not in rc


def test_get_radio_for_board_kiss_forwards_agc_and_fem(monkeypatch):
    captured = _kiss_capture_radio_config(monkeypatch)

    board_config = {
        "radio_type": "kiss",
        "kiss": {
            "port": "/dev/ttyACM0",
            "agc_reset_interval_seconds": 4,
            "fem_rx_gain": True,
            "fem_tx_gain": False,
            "rx_boosted_gain": False,
        },
        "repeater": {"agc_reset_interval": 60},  # legacy key loses to canonical
        "radio": _modem_radio_cfg(),
    }

    get_radio_for_board(board_config)

    rc = captured["kwargs"]["radio_config"]
    assert rc["agc_reset_interval_seconds"] == 4
    assert rc["fem_rx_gain"] is True
    assert rc["fem_tx_gain"] is False
    assert rc["rx_boosted_gain"] is False


def test_kiss_hardware_config_legacy_agc_fallback_and_bounds():
    from repeater.config import kiss_hardware_config

    assert kiss_hardware_config({"repeater": {"agc_reset_interval": 8}}) == {
        "agc_reset_interval_seconds": 8
    }
    # Above the KISS limit is passed through; the radio enforces its own range.
    assert kiss_hardware_config({"kiss": {"agc_reset_interval_seconds": 9999}}) == {
        "agc_reset_interval_seconds": 9999
    }
    # A bad value is dropped rather than stopping the radio from being built.
    assert kiss_hardware_config({"kiss": {"agc_reset_interval_seconds": "soon"}}) == {}
    assert kiss_hardware_config({"kiss": {"agc_reset_interval_seconds": True}}) == {}
    assert kiss_hardware_config({"kiss": {"agc_reset_interval_seconds": -1}}) == {}
    assert kiss_hardware_config({"kiss": {"port": "/dev/x"}}) == {}


def test_radio_frontend_config_reads_the_modem_section_not_kiss():
    from repeater.config import radio_frontend_config

    modem = {
        "agc_reset_interval_seconds": 30,
        "fem_tx_gain": True,
        "rx_boosted_gain": False,
    }
    kiss = {"agc_reset_interval_seconds": 8, "fem_rx_gain": True}
    assert (
        radio_frontend_config(
            {"radio_type": "modem_tcp", "modem_tcp": {"host": "m", **modem}, "kiss": kiss}
        )
        == modem
    )
    assert (
        radio_frontend_config(
            {
                "radio_type": "modem_usb",
                "modem_usb": {"port": "/dev/ttyACM0", **modem},
                "kiss": kiss,
            }
        )
        == modem
    )
    # Legacy repeater key fills in only when the radio's own section has no AGC key.
    assert radio_frontend_config(
        {
            "radio_type": "modem_usb",
            "modem_usb": {"port": "/dev/ttyACM0"},
            "kiss": kiss,
            "repeater": {"agc_reset_interval": 30},
        }
    ) == {"agc_reset_interval_seconds": 30}


@pytest.mark.parametrize("radio_type", ["modem_tcp", "modem_usb"])
def test_modem_constructor_receives_its_frontend_config(monkeypatch, radio_type):
    captured = _capture_modem(monkeypatch, radio_type)
    board = _modem_board(radio_type, _modem_radio_cfg())
    board[radio_type]["agc_reset_interval_seconds"] = 2000
    board[radio_type]["fem_rx_gain"] = True
    board["kiss"] = {"agc_reset_interval_seconds": 8, "fem_tx_gain": True}

    get_radio_for_board(board)

    assert captured["radio_config"] == {
        "agc_reset_interval_seconds": 2000,
        "fem_rx_gain": True,
    }


@pytest.mark.parametrize("radio_type", ["modem_tcp", "modem_usb"])
def test_modem_constructor_omits_empty_frontend_config(monkeypatch, radio_type):
    captured = _capture_modem(monkeypatch, radio_type)
    board = _modem_board(radio_type, _modem_radio_cfg())
    board["kiss"] = {"agc_reset_interval_seconds": 8}

    get_radio_for_board(board)

    assert "radio_config" not in captured


@pytest.mark.parametrize("radio_type", ["modem_tcp", "modem_usb"])
@pytest.mark.parametrize("frontend_configured", [False, True])
def test_modem_builds_with_an_older_core(monkeypatch, caplog, radio_type, frontend_configured):
    module = pytest.importorskip(f"openhop_core.hardware.{radio_type[6:]}_radio")
    built = []
    started = []

    class ReleasedRadio:
        def __init__(
            self,
            port,
            frequency,
            bandwidth,
            spreading_factor,
            coding_rate,
            tx_power,
            sync_word,
            preamble_length,
            lbt_enabled,
            lbt_max_attempts,
            host=None,
            token=None,
            connect_timeout=None,
            baudrate=None,
        ):
            built.append(self)

        def begin(self):
            started.append(self)
            return True

    name = "TCPLoRaRadio" if radio_type == "modem_tcp" else "USBLoRaRadio"
    monkeypatch.setattr(module, name, ReleasedRadio)
    board = _modem_board(radio_type, _modem_radio_cfg())
    if frontend_configured:
        board[radio_type].update(agc_reset_interval_seconds=32, fem_rx_gain=True)

    with caplog.at_level("WARNING", logger="Config"):
        radio = get_radio_for_board(board)

    assert radio is not None
    assert len(built) == 1
    assert started == built
    if frontend_configured:
        assert "front-end settings" in caplog.text
        assert "need a newer openhop_core" in caplog.text
        assert "building radio without them" in caplog.text
        assert radio_type in caplog.text
    else:
        assert not caplog.records


@pytest.mark.parametrize(
    "module_name, class_name, maximum, step",
    [
        ("kiss_modem_wrapper", "KissModemWrapper", 1020, 4),
        ("tcp_radio", "TCPLoRaRadio", 3600, 1),
        ("usb_radio", "USBLoRaRadio", 3600, 1),
    ],
)
def test_agc_range_check_uses_the_core_wrapper(module_name, class_name, maximum, step):
    from repeater.config import agc_reset_interval_error

    module = pytest.importorskip(f"openhop_core.hardware.{module_name}")
    wrapper = getattr(module, class_name)

    for value in (0, 1, 10, maximum):
        assert agc_reset_interval_error(wrapper, value) is None
        assert wrapper.effective_agc_reset_interval(value) == value - value % step
    for value in (-1, maximum + 1, 5000):
        assert agc_reset_interval_error(wrapper, value) == (
            f"AGC reset interval {value} is outside this radio's range (0-{maximum} seconds)"
        )


# ─── sync word warning for USB/TCP modems ──────────────────────────────


def _capture_modem(monkeypatch, radio_type):
    module, cls = {
        "modem_tcp": ("openhop_core.hardware.tcp_radio", "TCPLoRaRadio"),
        "modem_usb": ("openhop_core.hardware.usb_radio", "USBLoRaRadio"),
    }[radio_type]
    pytest.importorskip(module)
    captured = {}

    class _Dummy(_DummyRadio):
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(f"{module}.{cls}", _Dummy)
    return captured


def _modem_board(radio_type, radio):
    section = {"host": "modem.local"} if radio_type == "modem_tcp" else {"port": "/dev/ttyACM0"}
    return {"radio_type": radio_type, radio_type: section, "radio": radio}


@pytest.mark.parametrize("radio_type", ["modem_tcp", "modem_usb"])
def test_modem_on_a_foreign_sync_word_is_warned_about_but_not_overridden(
    monkeypatch, caplog, radio_type
):
    """A modem on another sync word connects, answers pings and transmits but
    hears no MeshCore traffic, so say so -- without second-guessing the value."""
    captured = _capture_modem(monkeypatch, radio_type)
    radio = {**_modem_radio_cfg(), "sync_word": 0x3444}

    with caplog.at_level("WARNING", logger="Config"):
        get_radio_for_board(_modem_board(radio_type, radio))

    assert captured["sync_word"] == 0x3444
    assert "sync_word is 0x3444, not MeshCore's 0x12" in caplog.text
    assert radio_type in caplog.text


@pytest.mark.parametrize("radio_type", ["modem_tcp", "modem_usb"])
@pytest.mark.parametrize("sync_word", [0x12, "0x12", 18, None])
def test_modem_on_meshcores_sync_word_is_not_warned_about(
    monkeypatch, caplog, radio_type, sync_word
):
    captured = _capture_modem(monkeypatch, radio_type)
    radio = _modem_radio_cfg()
    if sync_word is None:
        radio.pop("sync_word")  # the default
    else:
        radio["sync_word"] = sync_word

    with caplog.at_level("WARNING", logger="Config"):
        get_radio_for_board(_modem_board(radio_type, radio))

    assert captured["sync_word"] == 0x12
    assert "sync_word" not in caplog.text


def test_a_radios_entry_inheriting_a_stray_top_level_sync_word_is_named(monkeypatch, caplog):
    """The case that bit a real node: a top-level radio.sync_word the SX1262
    driver ignores, inherited by a modem entry under radios[]."""
    from repeater.config import _merge_radio_entry

    captured = _capture_modem(monkeypatch, "modem_tcp")
    global_config = {"radio_type": "sx1262", "radio": {**_modem_radio_cfg(), "sync_word": 13380}}
    entry = {
        "id": "narrow",
        "radio_type": "modem_tcp",
        "radio": {"frequency": 910525000},
        "modem_tcp": {"host": "192.168.50.154"},
    }

    with caplog.at_level("WARNING", logger="Config"):
        get_radio_for_board(_merge_radio_entry(global_config, entry))

    assert captured["sync_word"] == 0x3444
    assert "Radio 'narrow' (modem_tcp) sync_word is 0x3444" in caplog.text
    assert "inherits the top-level radio.sync_word" in caplog.text


def test_a_radios_entry_overriding_the_stray_sync_word_is_not_warned_about(monkeypatch, caplog):
    """The fix for the inherited case: the entry sets 0x12 itself."""
    from repeater.config import _merge_radio_entry

    captured = _capture_modem(monkeypatch, "modem_tcp")
    global_config = {"radio_type": "sx1262", "radio": {**_modem_radio_cfg(), "sync_word": 13380}}
    entry = {
        "id": "narrow",
        "radio_type": "modem_tcp",
        "radio": {"frequency": 910525000, "sync_word": 0x12},
        "modem_tcp": {"host": "192.168.50.154"},
    }

    with caplog.at_level("WARNING", logger="Config"):
        get_radio_for_board(_merge_radio_entry(global_config, entry))

    assert captured["sync_word"] == 0x12
    assert "sync_word" not in caplog.text


@pytest.mark.parametrize("radio_type", ["modem_tcp", "modem_usb"])
def test_an_explicit_null_sync_word_means_the_default(monkeypatch, caplog, radio_type):
    """`sync_word:` left empty in YAML loads as None."""
    captured = _capture_modem(monkeypatch, radio_type)
    radio = {**_modem_radio_cfg(), "sync_word": None}

    with caplog.at_level("WARNING", logger="Config"):
        get_radio_for_board(_modem_board(radio_type, radio))

    assert captured["sync_word"] == 0x12
    assert "sync_word" not in caplog.text


def test_build_radio_stack_names_the_radios_entry_with_the_stray_sync_word(monkeypatch, caplog):
    """The real path: build_radio_stack pops _radio_id before building the
    radio, so the warning must still name the entry."""
    from repeater.config import build_radio_stack

    captured = _capture_modem(monkeypatch, "modem_tcp")
    config = {
        "radio_type": "sx1262",
        "radio": {**_modem_radio_cfg(), "sync_word": 13380},
        "radios": [
            {
                "id": "narrow",
                "radio_type": "modem_tcp",
                "radio": {"frequency": 910525000},
                "modem_tcp": {"host": "192.168.50.154"},
            }
        ],
    }

    with caplog.at_level("WARNING", logger="Config"):
        build_radio_stack(config)

    assert captured["sync_word"] == 0x3444
    assert "Radio 'narrow' (modem_tcp) sync_word is 0x3444" in caplog.text
    assert "inherits the top-level radio.sync_word" in caplog.text


def test_each_modem_entry_gets_its_own_sync_word_and_warning(monkeypatch, caplog):
    from repeater.config import build_radio_stack

    pytest.importorskip("openhop_core.hardware.tcp_radio")
    built = []

    class _Dummy(_DummyRadio):
        def __init__(self, **kwargs):
            built.append(kwargs)

    monkeypatch.setattr("openhop_core.hardware.tcp_radio.TCPLoRaRadio", _Dummy)
    config = {
        "radio_type": "sx1262",
        "radio": {**_modem_radio_cfg(), "sync_word": 13380},
        "radios": [
            {"id": "local", "radio_type": "modem_tcp", "modem_tcp": {"host": "10.0.0.1"}},
            {
                "id": "narrow",
                "radio_type": "modem_tcp",
                "radio": {"sync_word": 0x34},
                "modem_tcp": {"host": "10.0.0.2"},
            },
        ],
        "fabric": {"default_radio": "local"},
    }

    with caplog.at_level("WARNING", logger="Config"):
        build_radio_stack(config)

    assert [b["sync_word"] for b in built] == [0x3444, 0x34]
    lines = [r.getMessage() for r in caplog.records]
    local = next(m for m in lines if "Radio 'local'" in m)
    narrow = next(m for m in lines if "Radio 'narrow'" in m)
    assert "radios[local].radio (it inherits" in local
    assert "radios[narrow].radio." in narrow and "inherits" not in narrow
