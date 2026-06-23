from app.models import WorkflowRule


def test_workflow_rule_roundtrip(db_session):
    r = WorkflowRule(
        name="SEV1 playbook",
        trigger="incident.opened",
        conditions=[{"field": "severity", "op": "equals", "value": 1}],
        actions=[{"type": "open_slack_channel", "params": {}}],
    )
    db_session.add(r)
    db_session.flush()
    assert r.enabled is True and r.rank == 100
    assert r.conditions[0]["field"] == "severity"
    assert r.actions[0]["type"] == "open_slack_channel"
