"""Document session counters separately from persistent rolling usage."""

from pathlib import Path

import yaml


def test_mqtt_session_counters_are_documented_in_openapi():
    spec = yaml.safe_load((Path(__file__).parents[1] / "repeater/web/openapi.yaml").read_text())
    schema = spec["paths"]["/mqtt_status"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    sent = schema["properties"]["data"]["properties"]["brokers"]["items"]["properties"]["sent"]
    assert sent == {"$ref": "#/components/schemas/MqttSentCounters"}
    counters = spec["components"]["schemas"]["MqttSentCounters"]
    assert set(counters["required"]) == {"messages", "payload_bytes"}
    for name in counters["required"]:
        assert counters["properties"][name]["type"] == "integer"
        assert counters["properties"][name]["minimum"] == 0
    assert "restart" in counters["description"].lower()
    assert "Glass" in spec["paths"]["/mqtt_status"]["get"]["description"]
