from aws_cdk import CfnOutput
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct


class NetworkingConstruct(Construct):
    """VPC mreza bez NAT — Lambda funkcije u javnim subnetima."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.vpc = ec2.Vpc(
            self,
            "MedallionVpc",
            max_azs=2,
            nat_gateways=0,
            restrict_default_security_group=True,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
            ],
        )

        self.s3_endpoint = self.vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )

        self.ssm_endpoint = self.vpc.add_interface_endpoint(
            "SsmEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SSM,
            subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC,
            ),
            private_dns_enabled=True,
        )

        self.sg_lambda_pipeline = ec2.SecurityGroup(
            self,
            "LambdaPipelineSg",
            vpc=self.vpc,
            description="Bronze, silver i gold Lambda funkcije u javnim subnetima",
            allow_all_outbound=False,
        )
        self.sg_lambda_pipeline.add_egress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(443),
            "HTTPS ka vanjskim API-jima i SSM-u",
        )

        self.sg_sync_lambda = ec2.SecurityGroup(
            self,
            "SyncLambdaSg",
            vpc=self.vpc,
            description="Sync Lambda - S3 gold - PostgreSQL na EC2",
            allow_all_outbound=False,
        )
        self.sg_sync_lambda.add_egress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(443),
            "HTTPS ka SSM za dohvatanje parametara",
        )
        self.sg_sync_lambda.add_egress_rule(
            self.sg_ec2 if hasattr(self, 'sg_ec2') else ec2.Peer.any_ipv4(),
            ec2.Port.tcp(5432),
            "PostgreSQL na EC2",
        )

        self.sg_notify_lambda = ec2.SecurityGroup(
            self,
            "NotifyLambdaSg",
            vpc=self.vpc,
            description="Discord notifikacije",
            allow_all_outbound=False,
        )
        self.sg_notify_lambda.add_egress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(443),
            "HTTPS ka Discord webhook-u",
        )

        self.sg_ec2 = ec2.SecurityGroup(
            self,
            "Ec2SupersetSg",
            vpc=self.vpc,
            description="EC2 sa PostgreSQL i Apache Superset",
            allow_all_outbound=False,
        )
        self.sg_ec2.add_egress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(443),
            "HTTPS za Docker image pull",
        )
        self.sg_ec2.add_ingress_rule(
            self.sg_sync_lambda,
            ec2.Port.tcp(5432),
            "PostgreSQL samo od sync Lambda",
        )

        # Ovo mora biti NAKON što je sg_ec2 kreiran
        self.sg_sync_lambda.add_egress_rule(
            self.sg_ec2,
            ec2.Port.tcp(5432),
            "PostgreSQL na EC2",
        )

        CfnOutput(self, "VpcId", value=self.vpc.vpc_id, description="VPC ID")
        CfnOutput(
            self,
            "PublicSubnetIds",
            value=",".join(
                subnet.subnet_id for subnet in self.vpc.public_subnets
            ),
            description="Javni subneti za Lambda i EC2",
        )

    def pipeline_lambda_kwargs(self) -> dict:
        return {
            "vpc": self.vpc,
            "vpc_subnets": ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC,
            ),
            "security_groups": [self.sg_lambda_pipeline],
        }

    def notify_lambda_kwargs(self) -> dict:
        return {
            "vpc": self.vpc,
            "vpc_subnets": ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC,
            ),
            "security_groups": [self.sg_notify_lambda],
        }

    def sync_lambda_kwargs(self) -> dict:
        return {
            "vpc": self.vpc,
            "vpc_subnets": ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC,
            ),
            "security_groups": [self.sg_sync_lambda],
        }

    def restrict_bucket_to_vpc(self, bucket: s3.IBucket) -> None:
        bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyNonVpcEndpointAccess",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:*"],
                resources=[
                    bucket.bucket_arn,
                    bucket.arn_for_objects("*"),
                ],
                conditions={
                    "StringNotEquals": {
                        "aws:SourceVpce": self.s3_endpoint.vpc_endpoint_id,
                    },
                },
            )
        )