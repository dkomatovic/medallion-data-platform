from aws_cdk import CfnOutput
from aws_cdk import aws_ec2 as ec2
from constructs import Construct


class NetworkingConstruct(Construct):
    """VPC mreza za Medalion platformu (free-tier: NAT instance umesto NAT Gateway)."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Jedan NAT instance (t2.micro) umesto NAT Gateway-a (~$32/mesec)
        self.vpc = ec2.Vpc(
            self,
            "MedallionVpc",
            max_azs=2,
            nat_gateways=1,
            nat_gateway_provider=ec2.NatProvider.instance_v2(
                instance_type=ec2.InstanceType.of(
                    ec2.InstanceClass.T2,
                    ec2.InstanceSize.MICRO,
                ),
            ),
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

        # Besplatan pristup S3-u bez NAT-a (bronze/silver/gold/sync)
        self.s3_endpoint = self.vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )

        # --- Security groups (least privilege, dopunjavaju se u kasnijim fazama) ---

        self.sg_lambda_pipeline = ec2.SecurityGroup(
            self,
            "LambdaPipelineSg",
            vpc=self.vpc,
            description="Bronze, silver i gold Lambda funkcije u privatnim subnetima",
            allow_all_outbound=False,
        )
        self.sg_lambda_pipeline.add_egress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(443),
            "HTTPS ka spoljasnjim API-jima preko NAT instance",
        )
        # S3 gateway endpoint ne koristi SG — pristup S3-u ide preko route table

        self.sg_sync_lambda = ec2.SecurityGroup(
            self,
            "SyncLambdaSg",
            vpc=self.vpc,
            description="Sync Lambda — S3 gold -> PostgreSQL na EC2",
            allow_all_outbound=False,
        )
        # S3 gateway endpoint ne koristi SG — pristup S3-u ide preko route table

        self.sg_notify_lambda = ec2.SecurityGroup(
            self,
            "NotifyLambdaSg",
            vpc=self.vpc,
            description="Discord notifikacije na pad pipeline-a",
            allow_all_outbound=False,
        )
        self.sg_notify_lambda.add_egress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(443),
            "HTTPS ka Discord webhook-u preko NAT instance",
        )

        self.sg_ec2 = ec2.SecurityGroup(
            self,
            "Ec2SupersetSg",
            vpc=self.vpc,
            description="EC2 sa PostgreSQL i Apache Superset",
            allow_all_outbound=True,
        )
        self.sg_ec2.add_ingress_rule(
            self.sg_sync_lambda,
            ec2.Port.tcp(5432),
            "PostgreSQL samo od sync Lambda",
        )

        # Sync Lambda sme da se konektuje na Postgres na EC2
        self.sg_sync_lambda.add_egress_rule(
            self.sg_ec2,
            ec2.Port.tcp(5432),
            "PostgreSQL na EC2",
        )

        CfnOutput(self, "VpcId", value=self.vpc.vpc_id, description="VPC ID")
        CfnOutput(
            self,
            "PrivateSubnetIds",
            value=",".join(subnet.subnet_id for subnet in self.vpc.private_subnets),
            description="Privatni subneti za Lambda funkcije",
        )
        CfnOutput(
            self,
            "PublicSubnetIds",
            value=",".join(subnet.subnet_id for subnet in self.vpc.public_subnets),
            description="Javni subneti za NAT instance i EC2",
        )

    def pipeline_lambda_kwargs(self) -> dict:
        """VPC podešavanja za bronze/silver/gold Lambda funkcije."""
        return {
            "vpc": self.vpc,
            "vpc_subnets": ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
            ),
            "security_groups": [self.sg_lambda_pipeline],
        }

    def notify_lambda_kwargs(self) -> dict:
        """VPC podešavanja za Discord notify Lambda."""
        return {
            "vpc": self.vpc,
            "vpc_subnets": ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
            ),
            "security_groups": [self.sg_notify_lambda],
        }
