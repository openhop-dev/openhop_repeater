"""Ecowitt gateway sensor: local-API parsing and unit normalisation."""

import pytest

from repeater.sensors.ecowitt import EcowittGatewaySensor


def _gw(**overrides):
    """A minimal GW3000-class live-data document, metric units."""
    doc = {
        "common_list": [
            {"id": "0x02", "val": "9.7", "unit": "C"},
            {"id": "0x07", "val": "81%"},
            {"id": "0x03", "val": "6.6", "unit": "C"},
            {"id": "0x0A", "val": "71"},
            {"id": "0x0B", "val": "3.24 km/h"},
            {"id": "0x0C", "val": "5.40 km/h"},
            {"id": "0x19", "val": "27.00 km/h"},
            {"id": "0x15", "val": "100.0 W/m2"},
            {"id": "0x17", "val": "0"},
        ],
        "piezoRain": [
            {"id": "0x0D", "val": "0.0 mm"},
            {"id": "0x0E", "val": "0.0 mm/Hr"},
            {"id": "0x7C", "val": "0.6 mm"},
            {"id": "0x10", "val": "0.6 mm"},
            {"id": "0x11", "val": "0.6 mm"},
            {"id": "0x12", "val": "16.0 mm"},
            {
                "id": "0x13",
                "val": "577.2 mm",
                "battery": "2",
                "voltage": "2.54",
                "ws90cap_volt": "4.6",
                "ws90_ver": "156",
            },
        ],
        "wh25": [{"intemp": "20.6", "unit": "C", "inhumi": "58%", "abs": "993.0 hPa", "rel": "993.0 hPa"}],
        "lightning": [{"distance": "10 km", "count": "0", "battery": "2"}],
        "debug": [{"heap": "98412", "runtime": "2062232", "reset_reason": "3"}],
    }
    doc.update(overrides)
    return doc


class _Sensor(EcowittGatewaySensor):
    """Sensor with the HTTP call replaced by a fixed document."""

    def __init__(self, document, settings=None):
        super().__init__(name="weather", config={"settings": {"host": "192.168.0.244", **(settings or {})}})
        self._document = document

    def _fetch(self):
        return self._document


def read(document, settings=None):
    result = _Sensor(document, settings).read()
    assert result["ok"] is True, result
    return result["data"]


def test_metric_document_maps_to_flat_readings():
    data = read(_gw())
    assert data["temperature_c"] == 9.7
    assert data["humidity_pct"] == 81.0
    assert data["dewpoint_c"] == 6.6
    assert data["pressure_hpa"] == 993.0
    assert data["pressure_abs_hpa"] == 993.0
    assert data["indoor_temperature_c"] == 20.6
    assert data["indoor_humidity_pct"] == 58.0
    assert data["wind_speed_kmh"] == 3.24
    assert data["wind_gust_kmh"] == 5.4
    assert data["wind_dir_deg"] == 71.0
    assert data["rain_rate_mmh"] == 0.0
    assert data["rain_day_mm"] == 0.6
    assert data["rain_year_mm"] == 577.2
    assert data["lightning_distance_km"] == 10.0


def test_supply_voltage_comes_from_the_array_not_the_host():
    data = read(_gw())
    # The repeater uses bus_voltage_v as the *node's* pack voltage; the station
    # capacitor must never be reported there.
    assert "bus_voltage_v" not in data
    assert data["supply_voltage_v"] == 4.6
    assert data["station_capacitor_v"] == 4.6
    assert data["station_battery"] == 2


def test_irradiance_and_illuminance_are_kept_apart():
    data = read(_gw())
    assert data["solar_wm2"] == 100.0
    assert "illuminance_lux" not in data


def test_lux_reporting_gateway_populates_illuminance():
    doc = _gw(common_list=[{"id": "0x15", "val": "800 lux"}])
    data = read(doc)
    assert data["illuminance_lux"] == 800.0
    assert "solar_wm2" not in data


def test_footcandles_are_converted_to_lux():
    doc = _gw(common_list=[{"id": "0x15", "val": "10 fc"}])
    data = read(doc)
    assert data["illuminance_lux"] == pytest.approx(107.639, rel=1e-3)


def test_imperial_units_are_normalised():
    doc = _gw(
        common_list=[
            {"id": "0x02", "val": "50.0", "unit": "F"},
            {"id": "0x07", "val": "81%"},
            {"id": "0x0B", "val": "10.0 mph"},
        ],
        piezoRain=[{"id": "0x7C", "val": "0.5 in"}, {"id": "0x0E", "val": "0.2 in/hr"}],
        wh25=[{"intemp": "68.0", "unit": "F", "inhumi": "58%", "abs": "29.32 inHg", "rel": "29.32 inHg"}],
    )
    data = read(doc)
    assert data["temperature_c"] == pytest.approx(10.0, abs=0.01)
    assert data["wind_speed_kmh"] == pytest.approx(16.09, abs=0.01)
    assert data["rain_day_mm"] == pytest.approx(12.7, abs=0.01)
    assert data["rain_rate_mmh"] == pytest.approx(5.08, abs=0.01)  # in/hr handled
    assert data["pressure_hpa"] == pytest.approx(992.9, abs=0.2)


def test_kilopascals_are_converted_to_hectopascals():
    doc = _gw(wh25=[{"intemp": "20.0", "unit": "C", "inhumi": "50%", "abs": "99.3 kPa", "rel": "99.3 kPa"}])
    assert read(doc)["pressure_hpa"] == pytest.approx(993.0, abs=0.01)


def test_non_numeric_entries_are_skipped_not_fatal():
    doc = _gw(common_list=[{"id": "0x02", "val": "--"}, {"id": "0x07", "val": "81%"}])
    data = read(doc)
    assert "temperature_c" not in data
    assert data["humidity_pct"] == 81.0


def test_empty_document_is_an_error():
    result = _Sensor({}).read()
    assert result["ok"] is False
    assert "no recognised fields" in result["error"]


@pytest.mark.parametrize(
    "host,port,expected",
    [
        ("192.168.0.244", 80, "http://192.168.0.244:80/get_livedata_info?cmd=get_livedata_info"),
        ("weather.lan", 80, "http://weather.lan:80/get_livedata_info?cmd=get_livedata_info"),
        ("fe80::1", 80, "http://[fe80::1]:80/get_livedata_info?cmd=get_livedata_info"),
        ("[::1]", 80, "http://[::1]:80/get_livedata_info?cmd=get_livedata_info"),
        ("[::1]:8080", 80, "http://[::1]:8080/get_livedata_info?cmd=get_livedata_info"),
        ("host:9000", 80, "http://host:9000/get_livedata_info?cmd=get_livedata_info"),
    ],
)
def test_host_forms_build_a_valid_url(host, port, expected):
    sensor = EcowittGatewaySensor(name="w", config={"settings": {"host": host, "port": port}})
    assert sensor.url == expected


def test_host_is_required():
    with pytest.raises(ValueError):
        EcowittGatewaySensor(name="w", config={"settings": {}})
