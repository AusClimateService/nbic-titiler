"""Construct App."""

import os
import shutil
from typing import Any, Dict, List, Optional, Union

from aws_cdk import App, CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_apigatewayv2_alpha as apigw
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_ecs_patterns as ecs_patterns
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda
from aws_cdk import aws_logs as logs
from aws_cdk.aws_apigatewayv2_integrations_alpha import HttpLambdaIntegration
from aws_cdk import aws_wafv2 as wafv2
from aws_cdk import aws_ecr, aws_ecr_assets  # <-- Added this import
import aws_cdk as cdk
from config import StackSettings
from constructs import Construct

settings = StackSettings()

class titilerLambdaStack(Stack):
    """
    Titiler Lambda Stack

    This code is freely adapted from
    - https://github.com/leothomas/titiler/blob/10df64fbbdd342a0762444eceebaac18d8867365/stack/app.py author: @leothomas
    - https://github.com/ciaranevans/titiler/blob/3a4e04cec2bd9b90e6f80decc49dc3229b6ef569/stack/app.py author: @ciaranevans

    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        memory: int = 1024,
        timeout: int = 30,
        runtime: aws_lambda.Runtime = aws_lambda.Runtime.FROM_IMAGE,
        concurrent: Optional[int] = None,
        permissions: Optional[List[iam.PolicyStatement]] = None,
        environment: Optional[Dict] = None,
        code_dir: str = "./",
        **kwargs: Any,
    ) -> None:
        """Define stack."""
        super().__init__(scope, id, **kwargs)

        permissions = permissions or []
        environment = environment or {}
       
        lambda_dockerfile = 'lambda/Dockerfile'
        install_type = "prod"

        # Create an ECR repository
        #ecr_repository = aws_ecr.Repository(
        #    self, 
        #    f"{id}-ecr-repo",
        #    removal_policy=cdk.RemovalPolicy.DESTROY  # Ensures the ECR repository is destroyed when the stack is destroyed
        #)

        if os.environ.get("TITILER_STACK_DEV_MODE") == "true":
            src_dir = "../../src/titiler"
            destination = os.path.join(code_dir, "titiler")
            # Remove destination directory if it exists
            if os.path.exists(destination):
                shutil.rmtree(destination) 
            # Copy the directory
            shutil.copytree(src_dir, destination)
            lambda_dockerfile = 'lambda/Dockerfile'
            install_type = "dev"

        # CDK asset to build and push Docker image to ECR
        docker_image_asset = aws_ecr_assets.DockerImageAsset(
            self,
            f"{id}-docker-asset",
            directory=os.path.abspath(code_dir),
            file=lambda_dockerfile,
            build_args={
                "INSTALL_TYPE": install_type
            },
        )

        cdk.CfnOutput(self, "Image URI", value=docker_image_asset.image_uri)
        lambda_function = aws_lambda.Function(
            self,
            f"{id}-lambda",
            runtime=runtime,
            code=aws_lambda.Code.from_ecr_image(
                repository=docker_image_asset.repository,
                #tag=docker_image_asset.image_uri.split(':')[-1]  # Use the tag from the DockerImageAsset
                tag=docker_image_asset.asset_hash
                #tag=docker_image_asset.image_uri.split(':')[-1].split(':')[-1]
            ),
            handler=aws_lambda.Handler.FROM_IMAGE,
            memory_size=memory,
            reserved_concurrent_executions=concurrent,
            timeout=Duration.seconds(timeout),
            environment=environment,
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        for perm in permissions:
            lambda_function.add_to_role_policy(perm)

        api = apigw.HttpApi(
            self,
            f"{id}-endpoint",
            default_integration=HttpLambdaIntegration(
                f"{id}-integration", handler=lambda_function
            ),
        )
        CfnOutput(self, "Endpoint", value=api.url)
        
        if os.environ.get("TITILER_STACK_DEV_MODE") == "true":
            # Remove destination directory if it exists
            if os.path.exists(destination):
                shutil.rmtree(destination) 

class titilerECSStack(Stack):
    """Titiler ECS Fargate Stack."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        cpu: Union[int, float] = 256,
        memory: Union[int, float] = 512,
        mincount: int = 1,
        maxcount: int = 50,
        permissions: Optional[List[iam.PolicyStatement]] = None,
        environment: Optional[Dict] = None,
        code_dir: str = "./",
        **kwargs: Any,
    ) -> None:
        """Define stack."""
        super().__init__(scope, id, *kwargs)

        permissions = permissions or []
        environment = environment or {}

        vpc = ec2.Vpc(self, f"{id}-vpc", max_azs=2)

        cluster = ecs.Cluster(self, f"{id}-cluster", vpc=vpc)

        task_env = environment.copy()
        task_env.update({"LOG_LEVEL": "error"})

        # GUNICORN configuration
        if settings.workers_per_core:
            task_env.update({"WORKERS_PER_CORE": str(settings.workers_per_core)})
        if settings.max_workers:
            task_env.update({"MAX_WORKERS": str(settings.max_workers)})
        if settings.web_concurrency:
            task_env.update({"WEB_CONCURRENCY": str(settings.web_concurrency)})

        fargate_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            f"{id}-service",
            cluster=cluster,
            cpu=cpu,
            memory_limit_mib=memory,
            desired_count=mincount,
            public_load_balancer=True,
            listener_port=80,
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_registry(
                    f"ghcr.io/developmentseed/titiler:{settings.image_version}",
                ),
                container_port=80,
                environment=task_env,
            ),
        )
        fargate_service.target_group.configure_health_check(path="/healthz")

        for perm in permissions:
            fargate_service.task_definition.task_role.add_to_policy(perm)

        scalable_target = fargate_service.service.auto_scale_task_count(
            min_capacity=mincount, max_capacity=maxcount
        )

        # https://github.com/awslabs/aws-rails-provisioner/blob/263782a4250ca1820082bfb059b163a0f2130d02/lib/aws-rails-provisioner/scaling.rb#L343-L387
        scalable_target.scale_on_request_count(
            "RequestScaling",
            requests_per_target=50,
            scale_in_cooldown=Duration.seconds(240),
            scale_out_cooldown=Duration.seconds(30),
            target_group=fargate_service.target_group,
        )

        # scalable_target.scale_on_cpu_utilization(
        #     "CpuScaling", target_utilization_percent=70,
        # )

        fargate_service.service.connections.allow_from_any_ipv4(
            port_range=ec2.Port(
                protocol=ec2.Protocol.ALL,
                string_representation="All port 80",
                from_port=80,
            ),
            description="Allows traffic on port 80 from ALB",
        )


app = App()

perms = []
if settings.buckets:
    perms.append(
        iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[
                f"arn:aws:s3:::{bucket}/{settings.key}" for bucket in settings.buckets
            ],
        )
    )


ecs_stack = titilerECSStack(
    app,
    f"{settings.name}-ecs-{settings.stage}",
    cpu=settings.task_cpu,
    memory=settings.task_memory,
    mincount=settings.min_ecs_instances,
    maxcount=settings.max_ecs_instances,
    permissions=perms,
    environment=settings.env,
)

lambda_stack = titilerLambdaStack(
    app,
    f"{settings.name}-lambda-{settings.stage}",
    memory=settings.memory,
    timeout=settings.timeout,
    concurrent=settings.max_concurrent,
    permissions=perms,
    environment=settings.env,
)

# Tag infrastructure
for key, value in {
    "Project": settings.name,
    "Stack": settings.stage,
    "Owner": settings.owner,
    "Client": settings.client,
}.items():
    if value:
        Tags.of(ecs_stack).add(key, value)
        Tags.of(lambda_stack).add(key, value)


app.synth()
