import base64
from pathlib import Path

from aws_cdk import CfnOutput, CfnParameter, Duration
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from medallion_data_platform.constructs.networking import NetworkingConstruct

EC2_DIR = Path(__file__).resolve().parent.parent.parent / "ec2"


class VisualizationConstruct(Construct):
    """EC2 (PostgreSQL + Superset) i sync Lambda za gold vizualizaciju."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        network: NetworkingConstruct,
        bucket: s3.IBucket,
        sdk_pandas_layer: lambda_.ILayerVersion,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        superset_cidr = CfnParameter(
            self,
            "SupersetAllowedCidr",
            type="String",
            default="0.0.0.0/0",
            description="CIDR allowed to access Superset on port 8088 (use your-public-ip/32)",
        )

        network.sg_ec2.add_ingress_rule(
            ec2.Peer.ipv4(superset_cidr.value_as_string),
            ec2.Port.tcp(8088),
            "Superset UI",
        )

        self.postgres_password_param = ssm.StringParameter(
            self,
            "PostgresPassword",
            parameter_name="/medallion/postgres/password",
            string_value="MedallionPgChangeMe1",
            type=ssm.ParameterType.SECURE_STRING,
            description="PostgreSQL password — promeniti posle prvog deploy-a",
        )

        self.superset_admin_password_param = ssm.StringParameter(
            self,
            "SupersetAdminPassword",
            parameter_name="/medallion/superset/admin-password",
            string_value="MedallionAdminChangeMe1",
            type=ssm.ParameterType.SECURE_STRING,
            description="Superset admin lozinka — promeniti posle prvog deploy-a",
        )

        self.postgres_host_param = ssm.StringParameter(
            self,
            "PostgresHost",
            parameter_name="/medallion/postgres/host",
            string_value="UNSET",
            description="PostgreSQL host — automatski postavlja EC2 user data",
        )

        instance_role = iam.Role(
            self,
            "SupersetInstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
            ],
        )
        self.postgres_password_param.grant_read(instance_role)
        self.superset_admin_password_param.grant_read(instance_role)
        self.postgres_host_param.grant_write(instance_role)

        self.ec2_instance = ec2.Instance(
            self,
            "SupersetInstance",
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.T2,
                ec2.InstanceSize.MICRO,
            ),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            vpc=network.vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC,
            ),
            security_group=network.sg_ec2,
            role=instance_role,
            user_data=self._build_user_data(),
            require_imdsv2=True,
        )

        eip = ec2.CfnEIP(self, "SupersetEip", domain="vpc")
        ec2.CfnEIPAssociation(
            self,
            "SupersetEipAssociation",
            allocation_id=eip.attr_allocation_id,
            instance_id=self.ec2_instance.instance_id,
        )

        self.sync_lambda = lambda_.Function(
            self,
            "GoldToPostgresSync",
            function_name="gold-to-postgres-sync",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                "lambdas/sync/gold_to_postgres",
                bundling={
                    "image": lambda_.Runtime.PYTHON_3_12.bundling_image,
                    "command": [
                        "bash",
                        "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
                    ],
                },
            ),
            timeout=Duration.minutes(5),
            memory_size=1024,
            layers=[sdk_pandas_layer],
            environment={
                "S3_BUCKET_NAME": bucket.bucket_name,
                "POSTGRES_HOST_PARAM": self.postgres_host_param.parameter_name,
                "POSTGRES_PASSWORD_PARAM": self.postgres_password_param.parameter_name,
                "POSTGRES_DB": "medallion",
                "POSTGRES_USER": "medallion",
                "POSTGRES_SCHEMA": "gold",
            },
            **network.sync_lambda_kwargs(),
        )
        bucket.grant_read(self.sync_lambda)
        self.postgres_password_param.grant_read(self.sync_lambda)
        self.postgres_host_param.grant_read(self.sync_lambda)

        CfnOutput(
            self,
            "SupersetUrl",
            value=f"http://{eip.attr_public_ip}:8088",
            description="Apache Superset UI (login: admin)",
        )
        CfnOutput(
            self,
            "SupersetEc2PrivateIp",
            value=self.ec2_instance.instance_private_ip,
            description="EC2 private IP za PostgreSQL konekciju",
        )
        CfnOutput(
            self,
            "SupersetSetupHint",
            value=(
                "Login admin, then Data > Datasets > + Dataset > PostgreSQL medallion > schema gold"
            ),
            description="Koraci za povezivanje gold tabela u Superset-u",
        )

    def _build_user_data(self) -> ec2.UserData:
        ud = ec2.UserData.for_linux()
        ud.add_commands("mkdir -p /opt/medallion")

        for filename in (
            "docker-compose.yml",
            "init-db.sql",
            "superset_config.py",
            "user_data.sh",
        ):
            content = (EC2_DIR / filename).read_text(encoding="utf-8")
            encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
            ud.add_commands(
                f"echo '{encoded}' | base64 -d > /opt/medallion/{filename}"
            )

        ud.add_commands(
            "chmod +x /opt/medallion/user_data.sh",
            "/opt/medallion/user_data.sh",
        )
        return ud
