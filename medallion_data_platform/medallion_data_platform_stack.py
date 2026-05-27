from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_s3 as s3,
    aws_lambda as lambda_,
    aws_events as events,
    aws_events_targets as targets,
)
from constructs import Construct


class MedallionDataPlatformStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # S3 bucket za bronze layer (sirovi podaci)
        bronze_bucket = s3.Bucket(
            self, "BronzeBucket",
            bucket_name="medallion-bronze-data",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # Lambda funkcija za Hacker News
        hn_lambda = lambda_.Function(
            self, "HackerNewsLambda",
            function_name="hacker-news-collector",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                "lambdas/bronze/hacker_news",
                bundling={
                    "image": lambda_.Runtime.PYTHON_3_12.bundling_image,
                    "command": [
                        "bash", "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
                    ],
                },
            ),
            timeout=Duration.minutes(5),
            memory_size=256,
            environment={
                "S3_BUCKET_NAME": bronze_bucket.bucket_name,
            },
        )

        # Dozvola da Lambda pise u S3
        bronze_bucket.grant_write(hn_lambda)

        # EventBridge - pokrece Lambda svaki dan u 02:00 UTC
        events.Rule(
            self, "DailyHNSchedule",
            schedule=events.Schedule.cron(hour="2", minute="0"),
            targets=[targets.LambdaFunction(hn_lambda)],
        )
