import json
import os

import boto3
import requests

WEBHOOK_PARAM = os.environ.get("DISCORD_WEBHOOK_PARAM", "/medallion/discord-webhook-url")
AWS_REGION = os.environ.get("AWS_REGION", "eu-north-1")

STATUS_COLORS = {
    "FAILED": 0xE74C3C,
    "TIMED_OUT": 0xE67E22,
    "ABORTED": 0x95A5A6,
}


def _get_webhook_url():
    value = boto3.client("ssm").get_parameter(
        Name=WEBHOOK_PARAM,
        WithDecryption=True,
    )["Parameter"]["Value"]
    if not value or value == "UNSET":
        raise ValueError(
            f"Discord webhook nije konfigurisan. Postavite SSM parametar: {WEBHOOK_PARAM}"
        )
    return value


def _truncate(text, limit=900):
    if not text:
        return "—"
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _console_url(execution_arn):
    if not execution_arn:
        return None
    parts = execution_arn.split(":")
    if len(parts) < 8:
        return None
    region = parts[3]
    return (
        f"https://{region}.console.aws.amazon.com/states/home"
        f"?region={region}#/v2/executions/details/{execution_arn}"
    )


def _build_embed(detail):
    status = detail.get("status", "UNKNOWN")
    execution_name = detail.get("name", "n/a")
    state_machine_arn = detail.get("stateMachineArn", "n/a")
    execution_arn = detail.get("executionArn", "")
    error = detail.get("error", "")
    cause = detail.get("cause", "")

    fields = [
        {"name": "Status", "value": status, "inline": True},
        {"name": "Execution", "value": execution_name, "inline": True},
        {"name": "State machine", "value": _truncate(state_machine_arn, 200), "inline": False},
    ]
    if error:
        fields.append({"name": "Error", "value": _truncate(error), "inline": False})
    if cause:
        fields.append({"name": "Cause", "value": _truncate(cause), "inline": False})

    embed = {
        "title": "Medallion pipeline failed",
        "color": STATUS_COLORS.get(status, 0xE74C3C),
        "fields": fields,
    }

    console_url = _console_url(execution_arn)
    if console_url:
        embed["url"] = console_url

    return embed


def handler(event=None, context=None):
    """Salje Discord notifikaciju na Step Functions FAILED/TIMED_OUT/ABORTED."""
    detail = (event or {}).get("detail", {})
    status = detail.get("status", "UNKNOWN")
    print(f"Pipeline status event: {status}")

    webhook_url = _get_webhook_url()
    payload = {
        "username": "Medallion Pipeline",
        "embeds": [_build_embed(detail)],
    }

    response = requests.post(
        webhook_url,
        json=payload,
        timeout=10,
    )
    response.raise_for_status()

    print(f"Discord notifikacija poslata (HTTP {response.status_code})")
    return {"status": "sent", "pipeline_status": status}


if __name__ == "__main__":
    sample = {
        "detail": {
            "status": "FAILED",
            "name": "local-test",
            "stateMachineArn": "arn:aws:states:eu-north-1:123456789012:stateMachine:medallion-orchestrator",
            "executionArn": "arn:aws:states:eu-north-1:123456789012:execution:medallion-orchestrator:local-test",
            "error": "States.TaskFailed",
            "cause": "Test poruka",
        }
    }
    print(json.dumps(handler(sample), indent=2))
