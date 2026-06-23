import json

import pytest

from app.services import alert_adapters as A


def _cw_envelope(state="ALARM"):
    msg = {
        "AlarmName": "prod-cpu-high",
        "AlarmArn": "arn:aws:cloudwatch:eu-west-1:123:alarm:prod-cpu-high",
        "NewStateValue": state,
        "NewStateReason": "Threshold crossed",
        "Region": "EU (Ireland)",
    }
    return {
        "Type": "Notification",
        "Message": json.dumps(msg),
        "SigningCertURL": "https://x.amazonaws.com/c.pem",
        "Signature": "AAAA",
        "MessageId": "m1",
        "Timestamp": "t",
        "TopicArn": "arn:topic",
    }


def test_parse_sns_cloudwatch_firing():
    out = A.parse_sns(_cw_envelope("ALARM"), verify=False)
    assert len(out) == 1
    n = out[0]
    assert n["source"] == "cloudwatch"
    assert n["dedup_key"] == "arn:aws:cloudwatch:eu-west-1:123:alarm:prod-cpu-high"
    assert n["status"] == "firing"
    assert "prod-cpu-high" in n["title"]
    assert any("eu-west-1" in lk["url"] for lk in n["links"])


def test_parse_sns_cloudwatch_insufficient_data_is_firing():
    assert A.parse_sns(_cw_envelope("INSUFFICIENT_DATA"), verify=False)[0]["status"] == "firing"


def test_parse_sns_non_cloudwatch_message_best_effort():
    inner = {
        "detail-type": "ECS Task State Change",
        "id": "evt-1",
        "detail": {"lastStatus": "STOPPED"},
    }
    env = {
        "Type": "Notification",
        "Message": json.dumps(inner),
        "SigningCertURL": "https://x.amazonaws.com/c.pem",
        "Signature": "AAAA",
    }
    out = A.parse_sns(env, verify=False)
    assert len(out) == 1
    n = out[0]
    assert n["source"] == "eventbridge"
    assert n["dedup_key"] == "evt-1"
    assert n["title"] == "ECS Task State Change"
    assert set(n.keys()) == {
        "source",
        "dedup_key",
        "title",
        "description",
        "severity_raw",
        "status",
        "links",
        "payload",
    }


def test_parse_sns_cloudwatch_resolved_on_ok():
    assert A.parse_sns(_cw_envelope("OK"), verify=False)[0]["status"] == "resolved"


def test_parse_sns_subscription_confirmation():
    env = {
        "Type": "SubscriptionConfirmation",
        "SubscribeURL": "https://sns.amazonaws.com/confirm?x=1",
    }
    assert A.parse_sns(env, verify=False) == {
        "confirm_url": "https://sns.amazonaws.com/confirm?x=1"
    }


def test_parse_sns_bad_signature_raises():
    with pytest.raises(ValueError):
        A.parse_sns(_cw_envelope(), verify=True, fetch_cert=lambda url: b"not-a-cert")


def test_parse_alertmanager_firing_and_resolved():
    body = {
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "fp1",
                "labels": {"alertname": "PodCrash", "severity": "critical"},
                "annotations": {"summary": "pod crashing", "runbook_url": "https://rb"},
            },
            {
                "status": "resolved",
                "fingerprint": "fp2",
                "labels": {"alertname": "DiskFull"},
                "annotations": {},
            },
        ]
    }
    out = A.parse_alertmanager(body)
    assert {o["dedup_key"] for o in out} == {"fp1", "fp2"}
    fp1 = next(o for o in out if o["dedup_key"] == "fp1")
    assert fp1["status"] == "firing" and fp1["severity_raw"] == "critical"
    assert "PodCrash" in fp1["title"] and any(lk["url"] == "https://rb" for lk in fp1["links"])
    assert next(o for o in out if o["dedup_key"] == "fp2")["status"] == "resolved"


def test_parse_generic_with_mapping():
    settings = {
        "dedup_key": "id",
        "title": "name",
        "severity": "sev",
        "status": "state",
        "resolved_value": "ok",
    }
    out = A.parse_generic(
        {"id": "x1", "name": "Boom", "sev": "warn", "state": "alerting"}, settings
    )
    assert out[0]["dedup_key"] == "x1" and out[0]["status"] == "firing"
    out2 = A.parse_generic({"id": "x1", "name": "Boom", "state": "ok"}, settings)
    assert out2[0]["status"] == "resolved"


def test_parse_generic_missing_dedup_key_raises():
    with pytest.raises(ValueError):
        A.parse_generic({"name": "Boom"}, {"dedup_key": "id", "title": "name"})


def test_alertmanager_drops_non_http_link():
    body = {
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "fp",
                "labels": {"alertname": "X"},
                "annotations": {"runbook_url": "javascript:alert(1)"},
                "generatorURL": "https://ok",
            }
        ]
    }
    links = A.parse_alertmanager(body)[0]["links"]
    # the javascript: runbook is dropped; only the http(s) generatorURL survives
    assert [lk["url"] for lk in links] == ["https://ok"]


def test_subscription_confirmation_without_url_raises():
    with pytest.raises(ValueError):
        A.parse_sns({"Type": "SubscriptionConfirmation"}, verify=False)
