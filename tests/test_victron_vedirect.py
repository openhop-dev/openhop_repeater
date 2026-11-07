"""VE.Direct protocol fixtures are synthetic, not controller captures."""

import importlib
import os
import select
import threading
import time
from unittest.mock import MagicMock

import pytest

from repeater.sensors.manager import SensorManager


@pytest.mark.parametrize("checksum", range(256))
def test_parser_fragmented_binary_checksum_and_resynchronization(checksum):
    module = importlib.import_module("repeater.sensors.victron_vedirect")
    # Vary a synthetic ignored ASCII field to produce every possible checksum byte.
    base = {"V": 12800}
    for n in range(256):
        left = n
        chars = []
        for _ in range(5):
            step = min(left, 57)
            chars.append(chr(65 + step))
            left -= step
        base["X"] = "".join(chars)
        valid = frame(base)
        if valid[-1] == checksum:
            break
    else:
        pytest.fail("fixture could not generate checksum")
    interrupted = valid[:5] + b":A12345\n" + valid[5:]
    stream = b"junk\r\nV\t999\r\nChecksum\t\x00" + interrupted
    parser = module.VEDirectParser()
    found = []
    for byte in stream:
        found.extend(parser.feed(bytes([byte])))
    assert found == [{"V": "12800", "X": base["X"]}]


def frame(fields):
    body = b"".join(b"\r\n" + k.encode() + b"\t" + str(v).encode() for k, v in fields.items())
    body += b"\r\nChecksum\t"
    return body + bytes([-sum(body) % 256])


def test_parser_bounds_and_rejects_malformed_blocks():
    module = importlib.import_module("repeater.sensors.victron_vedirect")
    parser = module.VEDirectParser()
    assert parser.feed(b"garbage" * 10000) == []
    assert len(parser.buffer) <= 2048
    for fields in ({"TOOLONGLABEL": 1}, {"V": "1" * 33}, {f"X{i}": i for i in range(23)}):
        assert parser.feed(frame(fields)) == []
    duplicate = b"\r\nV\t1\r\nV\t2\r\nChecksum\t"
    assert parser.feed(duplicate + bytes([-sum(duplicate) % 256])) == []
    assert parser.feed(frame({"V": 12345})) == [{"V": "12345"}]


def manager(device="/dev/ttyUSB-test", **settings):
    return SensorManager(
        {
            "sensors": {
                "enabled": True,
                "auto_install_packages": False,
                "definitions": [
                    {
                        "type": "victron_vedirect",
                        "name": "solar",
                        "settings": {
                            "serial_device": device,
                            "read_timeout_seconds": 0.05,
                            **settings,
                        },
                    }
                ],
            }
        }
    )


def test_a_port_that_takes_the_whole_window_to_open_is_still_read(monkeypatch):
    import serial

    port = MagicMock()
    port.__enter__.return_value = port
    port.read.return_value = frame({"V": "12800"})

    def open_slowly(**kwargs):
        time.sleep(0.06)
        return port

    monkeypatch.setattr(serial, "Serial", MagicMock(side_effect=open_slowly))
    reading = manager().read_all()[0]
    assert reading["ok"], reading
    assert port.read.call_count == 1


@pytest.mark.parametrize(
    "label,raw,key,expected",
    [
        ("VPV", "18600", "pv_voltage_v", 18.6),
        ("PPV", "234", "pv_power_w", 234),
        ("IL", "1500", "load_current_a", 1.5),
        ("ERR", "17", "error_code", 17),
        ("MPPT", "2", "tracker_state_code", 2),
        ("HSDS", "364", "day_sequence", 364),
        ("OR", "0x00000040", "off_reason", 64),
        ("LOAD", "ON", "load_output", True),
        ("Relay", "Off", "relay", False),
        ("Alarm", "On", "alarm", True),
        ("AR", "5", "alarm_reason", 5),
        ("VS", "12400", "aux_voltage_v", 12.4),
        ("VM", "6200", "midpoint_voltage_v", 6.2),
        ("DM", "15", "midpoint_deviation_percent", 1.5),
        ("T", "25", "battery_temperature_c", 25),
        ("P", "-123", "battery_power_w", -123),
        ("CE", "-12345", "consumed_ah", -12.345),
        ("SOC", "755", "state_of_charge_percent", 75.5),
        ("TTG", "120", "time_to_go_minutes", 120),
        ("H1", "-2000", "deepest_discharge_ah", -2),
        ("H2", "-1000", "last_discharge_ah", -1),
        ("H3", "-1500", "average_discharge_ah", -1.5),
        ("H4", "10", "charge_cycles", 10),
        ("H5", "2", "full_discharges", 2),
        ("H6", "-5000", "cumulative_drawn_ah", -5),
        ("H7", "11000", "min_battery_voltage_v", 11),
        ("H8", "14500", "max_battery_voltage_v", 14.5),
        ("H9", "3600", "seconds_since_full_charge", 3600),
        ("H10", "5", "automatic_syncs", 5),
        ("H11", "3", "low_voltage_alarms", 3),
        ("H12", "4", "high_voltage_alarms", 4),
        ("H15", "10000", "min_aux_voltage_v", 10),
        ("H16", "14000", "max_aux_voltage_v", 14),
        ("H17", "123", "history_energy_h17_kwh", 1.23),
        ("H18", "234", "history_energy_h18_kwh", 2.34),
        ("MON", "-9", "monitor_mode_code", -9),
        ("H19", "456", "yield_total_kwh", 4.56),
        ("H20", "12", "yield_today_kwh", 0.12),
        ("H21", "300", "max_power_today_w", 300),
        ("H22", "23", "yield_yesterday_kwh", 0.23),
        ("H23", "350", "max_power_yesterday_w", 350),
    ],
)
def test_manager_protocol_normalization(monkeypatch, label, raw, key, expected):
    import serial

    port = MagicMock()
    port.__enter__.return_value = port
    port.read.return_value = frame({label: raw, "SER#": "synthetic-private", "FW": "123", "XYZ": 9})
    monkeypatch.setattr(serial, "Serial", MagicMock(return_value=port))
    reading = manager().read_all()[0]
    assert reading["ok"], reading
    assert reading["data"] == {key: expected}


@pytest.mark.parametrize(
    "fields,expected",
    [
        (
            {"V": "---", "I": 200, "T": "---", "SOC": "---", "CE": "---", "TTG": "---"},
            {
                "battery_voltage_v": None,
                "battery_current_a": 0.2,
                "battery_temperature_c": None,
                "state_of_charge_percent": None,
                "consumed_ah": None,
                "time_to_go_minutes": None,
            },
        ),
        ({"TTG": -1}, {"time_to_go_minutes": None}),
        (
            {"V": "nan", "SOC": 1001, "HSDS": 365, "LOAD": "bad", "I": "1_000"},
            {
                "battery_voltage_v": None,
                "state_of_charge_percent": None,
                "day_sequence": None,
                "load_output": None,
                "battery_current_a": None,
            },
        ),
    ],
)
def test_invalid_and_infinite_values_are_unavailable(monkeypatch, fields, expected):
    import serial

    port = MagicMock()
    port.__enter__.return_value = port
    port.read.return_value = frame(fields)
    monkeypatch.setattr(serial, "Serial", MagicMock(return_value=port))
    reading = manager().read_all()[0]
    assert reading["ok"], reading
    assert reading["data"] == expected


def test_metrics_are_current_snapshot_not_stale(monkeypatch):
    import serial

    port = MagicMock()
    port.__enter__.return_value = port
    port.read.return_value = frame({"V": 13000, "PPV": 25})
    monkeypatch.setattr(serial, "Serial", MagicMock(return_value=port))
    m = manager()
    first = m.read_all()[0]
    assert first["data"] == {"battery_voltage_v": 13.0, "pv_power_w": 25}
    assert all(metric["available"] for metric in first["metrics"])
    port.read.return_value = frame({"V": "---"})
    second = m.read_all()[0]
    assert second["data"] == {"battery_voltage_v": None}
    assert {metric["reason"] for metric in second["metrics"]} == {"invalid", "missing"}
    assert all(not metric["available"] for metric in second["metrics"])
    port.read.side_effect = OSError("synthetic-private-payload")
    failed = m.read_all()[0]
    assert failed["data"] == {} and not failed["ok"]
    assert all(metric["reason"] == "read_failed" for metric in failed["metrics"])
    assert "synthetic-private-payload" not in str(failed)


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"serial_device": ""},
        {"serial_device": "socket://host:1234"},
        {"serial_device": "/dev/x\x00"},
        {"serial_device": 1},
    ]
    + [
        {"serial_device": "/dev/x", "read_timeout_seconds": v}
        for v in (True, 0, -1, 2.1, "nan", "inf", None)
    ],
)
def test_settings_reject_invalid_before_open(settings):
    module = importlib.import_module("repeater.sensors.victron_vedirect")
    with pytest.raises(ValueError, match="serial_device|read_timeout_seconds"):
        module.VictronVEDirectSensor("test", {"settings": settings})


def test_type_discovery_exposes_settings_without_serial(monkeypatch):
    from repeater.web.api_endpoints import APIEndpoints

    module = importlib.import_module("repeater.sensors.victron_vedirect")
    real_import = module.importlib.import_module

    def imports(name, *args, **kwargs):
        if name == "serial":
            raise ImportError("dependency absent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(module.importlib, "import_module", imports)
    api = APIEndpoints.__new__(APIEndpoints)
    result = api.sensors_types()
    entry = next(x for x in result["data"]["types"] if x["type"] == "victron_vedirect")
    assert {x["key"] for x in entry["settings"]} == {"serial_device", "read_timeout_seconds"}
    m = manager()
    assert len(m.sensors) == 1
    reading = m.read_all()[0]
    assert not reading["ok"] and reading["data"] == {}
    assert "pyserial unavailable" in reading["error"]


def test_multiple_blocks_collected_only_within_this_poll(monkeypatch):
    import serial

    port = MagicMock()
    port.__enter__.return_value = port
    chunks = iter([frame({"V": 13000}), frame({"H19": 123}), b"\r\nV\t999"])
    port.read.side_effect = lambda size: next(chunks, b"")
    monkeypatch.setattr(serial, "Serial", MagicMock(return_value=port))
    m = manager(read_timeout_seconds=0.05)
    reading = m.read_all()[0]
    assert reading["data"] == {"battery_voltage_v": 13, "yield_total_kwh": 1.23}
    port.read.side_effect = None
    port.read.return_value = frame({"H20": 5})
    assert m.read_all()[0]["data"] == {"yield_today_kwh": 0.05}


def test_linux_pty_fragmented_frame_timeout_disconnect_recovery(monkeypatch, tmp_path):
    import serial

    master, slave = os.openpty()
    opened = threading.Event()
    real_serial = serial.Serial

    def factory(*args, **kwargs):
        port = real_serial(*args, **kwargs)
        opened.set()
        return port

    monkeypatch.setattr(serial, "Serial", factory)
    device_path = tmp_path / "vedirect"
    device_path.symlink_to(os.ttyname(slave))
    m = manager(str(device_path), read_timeout_seconds=0.2)
    assert len(m.sensors) == 1

    def producer():
        assert opened.wait(1)
        corrupt = frame({"V": 99999})[:-1] + b"\x00"
        payload = corrupt + frame({"V": 13100, "I": 400})
        for offset in range(0, len(payload), 3):
            os.write(master, payload[offset : offset + 3])
            time.sleep(0.002)

    thread = threading.Thread(target=producer)
    thread.start()
    try:
        reading = m.read_all()[0]
        thread.join(1)
        assert not thread.is_alive()
        assert reading["ok"], reading
        assert reading["data"]["battery_voltage_v"] == 13.1
        assert not select.select([master], [], [], 0)[0]  # no transmitted bytes
        started = time.monotonic()
        missing = m.read_all()[0]
        assert not missing["ok"] and missing["data"] == {}
        assert time.monotonic() - started < 0.6
        os.close(master)
        master = None
        assert not m.read_all()[0]["ok"]
        new_master, new_slave = os.openpty()
        try:
            device_path.unlink()
            device_path.symlink_to(os.ttyname(new_slave))
            opened.clear()

            def recovered():
                assert opened.wait(1)
                os.write(new_master, frame({"V": 12400}))

            thread = threading.Thread(target=recovered)
            thread.start()
            recovered_reading = m.read_all()[0]
            thread.join(1)
            assert recovered_reading["ok"], recovered_reading
            assert recovered_reading["data"] == {"battery_voltage_v": 12.4}
            assert not select.select([new_master], [], [], 0)[0]
        finally:
            os.close(new_master)
            os.close(new_slave)
    finally:
        thread.join(1)
        if master is not None:
            os.close(master)
        os.close(slave)


@pytest.mark.parametrize("failure", ["open", "read", "close"])
def test_transport_failures_close_and_recover(monkeypatch, failure):
    import serial

    port = MagicMock()
    port.__enter__.return_value = port
    port.read.return_value = frame({"V": 12000})
    factory = MagicMock(return_value=port)
    if failure == "open":
        factory.side_effect = PermissionError("sensitive-device-detail")
    elif failure == "read":
        port.read.side_effect = serial.SerialException("sensitive-device-detail")
    else:
        port.__exit__.side_effect = OSError("sensitive-device-detail")
    monkeypatch.setattr(serial, "Serial", factory)
    m = manager()
    reading = m.read_all()[0]
    assert not reading["ok"] and reading["data"] == {}
    assert "sensitive-device-detail" not in str(reading)
    if failure != "open":
        port.__exit__.assert_called_once()
    port.write.assert_not_called()
    factory.side_effect = None
    port.read.side_effect = None
    port.__exit__.side_effect = None
    assert m.read_all()[0]["data"] == {"battery_voltage_v": 12}


def test_byte_budget_and_corrupt_frame_never_published(monkeypatch):
    import serial

    port = MagicMock()
    port.__enter__.return_value = port
    port.read.return_value = b"x" * 256
    factory = MagicMock(return_value=port)
    monkeypatch.setattr(serial, "Serial", factory)
    m = manager(read_timeout_seconds=2)
    assert not m.read_all()[0]["ok"]
    assert port.read.call_count == 64
    assert all(call.args == (256,) for call in port.read.call_args_list)
    port.read.return_value = frame({"V": 12345})[:-1] + b"\x00"
    assert not m.read_all()[0]["ok"]
    port.write.assert_not_called()


def test_linux_pty_disconnect_during_read(monkeypatch):
    import serial

    master, slave = os.openpty()
    real_serial = serial.Serial
    opened = threading.Event()

    def factory(*args, **kwargs):
        port = real_serial(*args, **kwargs)
        opened.set()
        return port

    monkeypatch.setattr(serial, "Serial", factory)

    def unplug():
        assert opened.wait(1)
        os.close(master)

    thread = threading.Thread(target=unplug)
    thread.start()
    try:
        reading = manager(os.ttyname(slave), read_timeout_seconds=0.2).read_all()[0]
        assert not reading["ok"] and reading["data"] == {}
        assert "transport unavailable" in reading["error"]
    finally:
        thread.join(1)
        os.close(slave)
    assert not thread.is_alive()


def test_manager_reads_normalized_battery_without_writing(monkeypatch):
    import serial

    port = MagicMock()
    port.__enter__.return_value = port
    port.read.return_value = frame({"V": 12800, "I": -1250})
    factory = MagicMock(return_value=port)
    monkeypatch.setattr(serial, "Serial", factory)
    m = manager()
    assert len(m.sensors) == 1
    reading = m.read_all()[0]
    assert reading["ok"] is True
    assert reading["data"] == {"battery_voltage_v": 12.8, "battery_current_a": -1.25}
    port.write.assert_not_called()
    port.__exit__.assert_called_once()
    kwargs = factory.call_args.kwargs
    assert kwargs["baudrate"] == 19200
    assert kwargs["bytesize"] == 8
    assert kwargs["parity"] == "N"
    assert kwargs["stopbits"] == 1
    assert not kwargs["xonxoff"] and not kwargs["rtscts"] and not kwargs["dsrdtr"]


@pytest.mark.parametrize(
    "code,label",
    [
        (0, "Off"),
        (1, "Low power"),
        (2, "Fault"),
        (3, "Bulk"),
        (4, "Absorption"),
        (5, "Float"),
        (6, "Storage"),
        (7, "Equalize (manual)"),
        (9, "Inverting"),
        (11, "Power supply"),
        (245, "Starting-up"),
        (246, "Repeated absorption"),
        (247, "Auto equalize / Recondition"),
        (248, "BatterySafe"),
        (252, "External Control"),
        (999, "Unknown (999)"),
    ],
)
def test_manager_charge_state_is_decoded_for_existing_ui(monkeypatch, code, label):
    import serial

    port = MagicMock()
    port.__enter__.return_value = port
    port.read.return_value = frame({"CS": code})
    monkeypatch.setattr(serial, "Serial", MagicMock(return_value=port))
    reading = manager().read_all()[0]
    assert reading["ok"]
    assert reading["data"] == {"charge_state_code": code}
    descriptor = reading["metrics"][0]
    assert descriptor["label"] == f"Charge state — {label}"
    assert descriptor["category"] == "status"
    assert descriptor["kind"] == "number"


@pytest.mark.parametrize(
    "fields,expected",
    [({}, {}), ({"CS": "---"}, {"charge_state_code": None})],
)
def test_charge_state_label_missing_or_invalid_is_unavailable(fields, expected):
    from repeater.sensors.victron_vedirect import normalize

    assert normalize(fields) == expected
