import aws_cdk as core
import aws_cdk.assertions as assertions

from medallion_data_platform.medallion_data_platform_stack import MedallionDataPlatformStack


def test_vpc_and_s3_endpoint_created():
    app = core.App()
    stack = MedallionDataPlatformStack(app, "medallion-data-platform")
    template = assertions.Template.from_stack(stack)

    template.resource_count_is("AWS::EC2::VPC", 1)
    template.resource_count_is("AWS::EC2::VPCEndpoint", 1)
    template.resource_count_is("AWS::EC2::NatGateway", 0)


def test_security_groups_created():
    app = core.App()
    stack = MedallionDataPlatformStack(app, "medallion-data-platform")
    template = assertions.Template.from_stack(stack)

    # 4 aplikativna SG + 1 za NAT instance
    template.resource_count_is("AWS::EC2::SecurityGroup", 5)
    template.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        {"GroupDescription": "Bronze, silver i gold Lambda funkcije u privatnim subnetima"},
    )


def test_nat_instance_not_gateway():
    app = core.App()
    stack = MedallionDataPlatformStack(app, "medallion-data-platform")
    template = assertions.Template.from_stack(stack)

    template.resource_count_is("AWS::EC2::NatGateway", 0)
    template.resource_count_is("AWS::EC2::Instance", 1)


def test_pipeline_lambdas_in_vpc():
    app = core.App()
    stack = MedallionDataPlatformStack(app, "medallion-data-platform")
    template = assertions.Template.from_stack(stack)

    functions = template.find_resources("AWS::Lambda::Function")
    pipeline_names = {
        "hacker-news-collector",
        "x-dataset-collector",
        "silver-hacker-news",
        "silver-x",
        "gold-hacker-news",
        "gold-x",
    }
    in_vpc = [
        props
        for props in functions.values()
        if props.get("Properties", {}).get("FunctionName") in pipeline_names
    ]
    assert len(in_vpc) == 6
    for fn in in_vpc:
        assert "VpcConfig" in fn["Properties"]
        assert len(fn["Properties"]["VpcConfig"]["SubnetIds"]) >= 1
        assert len(fn["Properties"]["VpcConfig"]["SecurityGroupIds"]) >= 1


def test_discord_notification_resources():
    app = core.App()
    stack = MedallionDataPlatformStack(app, "medallion-data-platform")
    template = assertions.Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"FunctionName": "discord-pipeline-notify"},
    )
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {
            "Name": "/medallion/discord-webhook-url",
            "Type": "SecureString",
        },
    )
    template.has_resource_properties(
        "AWS::Events::Rule",
        {
            "Description": "Discord alarm na neuspesnom Step Functions run-u",
            "State": "ENABLED",
        },
    )

    functions = template.find_resources("AWS::Lambda::Function")
    notify = next(
        v for v in functions.values()
        if v["Properties"].get("FunctionName") == "discord-pipeline-notify"
    )
    assert "VpcConfig" in notify["Properties"]


def test_s3_bucket_secured_for_vpc():
    app = core.App()
    stack = MedallionDataPlatformStack(app, "medallion-data-platform")
    template = assertions.Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "BucketName": "medallion-bronze-data",
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
        },
    )
    template.has_resource_properties(
        "AWS::S3::BucketPolicy",
        {
            "PolicyDocument": assertions.Match.object_like({
                "Statement": assertions.Match.array_with([
                    assertions.Match.object_like({
                        "Sid": "DenyNonVpcEndpointAccess",
                        "Effect": "Deny",
                    }),
                ]),
            }),
        },
    )
