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
    layers=None,
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
        layers=layers or [],
    )
    bucket.grant_write(fn)
    return fn


class MedallionDataPlatformStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Javni AWS Lambda Layer sa awswrangler + pandas + pyarrow
        sdk_pandas_layer = lambda_.LayerVersion.from_layer_version_arn(
            self,
            "AWSSDKPandasLayer",
            "arn:aws:lambda:eu-north-1:336392948345:layer:AWSSDKPandas-Python312:18",
        )

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

        # Silver HN Lambda — cita bronze, upisuje silver
        silver_hn_lambda = make_lambda(
            self,
            "SilverHackerNewsLambda",
            function_name="silver-hacker-news",
            asset_path="lambdas/silver/hacker_news",
            bucket=bronze_bucket,
            timeout_minutes=15,
            memory=1024,
            layers=[sdk_pandas_layer],
        )
        bronze_bucket.grant_read(silver_hn_lambda)

        silver_x_lambda = make_lambda(
            self,
            "SilverXLambda",
            function_name="silver-x",
            asset_path="lambdas/silver/twitter",
            bucket=bronze_bucket,
            timeout_minutes=15,
            memory=1024,
            layers=[sdk_pandas_layer],
        )
        bronze_bucket.grant_read(silver_x_lambda)

        # Gold HN Lambda — cita silver, upisuje gold
        gold_hn_lambda = make_lambda(
            self,
            "GoldHackerNewsLambda",
            function_name="gold-hacker-news",
            asset_path="lambdas/gold/hacker_news",
            bucket=bronze_bucket,
            timeout_minutes=5,
            memory=1024,
            layers=[sdk_pandas_layer],
        )
        bronze_bucket.grant_read(gold_hn_lambda)

        gold_x_lambda = make_lambda(
            self,
            "GoldXLambda",
            function_name="gold-x",
            asset_path="lambdas/gold/twitter",
            bucket=bronze_bucket,
            timeout_minutes=5,
            memory=1024,
            layers=[sdk_pandas_layer],
        )
        bronze_bucket.grant_read(gold_x_lambda)

        # Step Functions — Bronze: HN i Twitter rade paralelno
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

        # Silver i Gold — rade sekvencijalno posle bronze-a
        silver_hn_task = sfn_tasks.LambdaInvoke(
            self,
            "SilverHackerNews",
            lambda_function=silver_hn_lambda,
            output_path="$.Payload",
        )

        gold_hn_task = sfn_tasks.LambdaInvoke(
            self,
            "GoldHackerNews",
            lambda_function=gold_hn_lambda,
            output_path="$.Payload",
        )

        silver_x_task = sfn_tasks.LambdaInvoke(
            self,
            "SilverX",
            lambda_function=silver_x_lambda,
            output_path="$.Payload",
        )

        gold_x_task = sfn_tasks.LambdaInvoke(
            self,
            "GoldX",
            lambda_function=gold_x_lambda,
            output_path="$.Payload",
        )

        parallel_silver = sfn.Parallel(self, "ParallelSilver")
        parallel_silver.branch(silver_hn_task)
        parallel_silver.branch(silver_x_task)

        parallel_gold = sfn.Parallel(self, "ParallelGold")
        parallel_gold.branch(gold_hn_task)
        parallel_gold.branch(gold_x_task)

        # Pipeline: parallel bronze → parallel_silver → parallel_gold
        pipeline = parallel_collect.next(parallel_silver).next(parallel_gold)

        state_machine = sfn.StateMachine(
            self,
            "MedallionOrchestrator",
            state_machine_name="medallion-orchestrator",
            definition_body=sfn.DefinitionBody.from_chainable(pipeline),
            timeout=Duration.hours(2),
        )

        # EventBridge - pokrece ceo pipeline svaki dan u 02:00 UTC
        events.Rule(
            self,
            "DailyMedallionSchedule",
            schedule=events.Schedule.cron(hour="2", minute="0"),
            targets=[targets.SfnStateMachine(state_machine)],
        )
