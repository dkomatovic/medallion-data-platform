from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as sfn_tasks
from aws_cdk.aws_lambda import DockerImageFunction, DockerImageCode
from constructs import Construct


def make_lambda(
    scope,
    construct_id,
    function_name,
    asset_path,
    bucket,
    timeout_minutes=15,
    memory=256,
):
    fn = lambda_.Function(
        scope,
        construct_id,
        function_name=function_name,
        runtime=lambda_.Runtime.PYTHON_3_12,
        handler="handler.handler",
        code=lambda_.Code.from_asset(
            asset_path,
            bundling={
                "image": lambda_.Runtime.PYTHON_3_12.bundling_image,
                "command": [
                    "bash",
                    "-c",
                    "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
                ],
            },
        ),
        timeout=Duration.minutes(timeout_minutes),
        memory_size=memory,
        environment={"S3_BUCKET_NAME": bucket.bucket_name},
    )
    bucket.grant_write(fn)
    return fn


class MedallionDataPlatformStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        bronze_bucket = s3.Bucket(
            self,
            "BronzeBucket",
            bucket_name="medallion-bronze-data",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        hn_lambda = make_lambda(
            self,
            "HackerNewsLambda",
            function_name="hacker-news-collector",
            asset_path="lambdas/bronze/hacker_news",
            bucket=bronze_bucket,
            timeout_minutes=15,
        )

        # X Lambda koristi Docker image zbog vecih zavisnosti (pandas, datasets)
        x_lambda = DockerImageFunction(
            self,
            "XLambda",
            function_name="x-dataset-collector",
            code=DockerImageCode.from_image_asset(
                directory="lambdas/bronze/twitter",
            ),
            timeout=Duration.minutes(10),
            memory_size=512,
            environment={
                "S3_BUCKET_NAME": bronze_bucket.bucket_name,
            },
        )
        bronze_bucket.grant_write(x_lambda)

        # Step Functions — HN i Twitter rade paralelno
        collect_hn = sfn_tasks.LambdaInvoke(
            self,
            "CollectHackerNews",
            lambda_function=hn_lambda,
            output_path="$.Payload",
        )

        collect_x = sfn_tasks.LambdaInvoke(
            self,
            "CollectX",
            lambda_function=x_lambda,
            output_path="$.Payload",
        )

        parallel_collect = sfn.Parallel(self, "ParallelCollect")
        parallel_collect.branch(collect_hn)
        parallel_collect.branch(collect_x)

        bronze_state_machine = sfn.StateMachine(
            self,
            "BronzeOrchestrator",
            state_machine_name="bronze-orchestrator",
            definition_body=sfn.DefinitionBody.from_chainable(parallel_collect),
            timeout=Duration.hours(1),
        )

        # EventBridge - pokrece oba u paraleli svaki dan u 02:00 UTC
        events.Rule(
            self,
            "DailyBronzeSchedule",
            schedule=events.Schedule.cron(hour="2", minute="0"),
            targets=[targets.SfnStateMachine(bronze_state_machine)],
        )
