from aws_cdk import Duration
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_ssm as ssm
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from medallion_data_platform.constructs.networking import NetworkingConstruct


class NotificationsConstruct(Construct):
    """Discord notifikacije kada Step Functions pipeline ne uspe."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        network: NetworkingConstruct,
        state_machine: sfn.IStateMachine,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        webhook_param = ssm.StringParameter(
            self,
            "DiscordWebhookUrl",
            parameter_name="/medallion/discord-webhook-url",
            string_value="UNSET",
            type=ssm.ParameterType.SECURE_STRING,
            description="Discord webhook URL za pipeline alarme (azurirati posle deploy-a)",
        )

        self.notify_lambda = lambda_.Function(
            self,
            "DiscordNotifyLambda",
            function_name="discord-pipeline-notify",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                "lambdas/notify/discord",
                bundling={
                    "image": lambda_.Runtime.PYTHON_3_12.bundling_image,
                    "command": [
                        "bash",
                        "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
                    ],
                },
            ),
            timeout=Duration.seconds(30),
            memory_size=128,
            environment={
                "DISCORD_WEBHOOK_PARAM": webhook_param.parameter_name,
            },
            **network.notify_lambda_kwargs(),
        )
        webhook_param.grant_read(self.notify_lambda)

        events.Rule(
            self,
            "PipelineFailureNotify",
            description="Discord alarm na neuspesnom Step Functions run-u",
            event_pattern=events.EventPattern(
                source=["aws.states"],
                detail_type=["Step Functions Execution Status Change"],
                detail={
                    "status": ["FAILED", "TIMED_OUT", "ABORTED"],
                    "stateMachineArn": [state_machine.state_machine_arn],
                },
            ),
            targets=[
                targets.LambdaFunction(
                    self.notify_lambda,
                    retry_attempts=2,
                    max_event_age=Duration.minutes(15),
                )
            ],
        )
