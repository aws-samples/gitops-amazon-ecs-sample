import boto3
import collections
import json
import logging
import os


DEFAULT_LOG_LEVEL = logging.DEBUG
REGION = os.environ['REGION']
ACCOUNT_ID = os.environ['ACCOUNT_ID']
sts_client = boto3.client('sts')
LOG_LEVELS = collections.defaultdict(
    lambda: DEFAULT_LOG_LEVEL,
    {
        "critical": logging.CRITICAL,
        "error": logging.ERROR,
        "warning": logging.WARNING,
        "info": logging.INFO,
        "debug": logging.DEBUG,
    },
)

# Lambda initializes a root logger that needs to be removed in order to set a
# different logging config
root = logging.getLogger()
if root.handlers:
    for handler in root.handlers:
        root.removeHandler(handler)

logging.basicConfig(level=LOG_LEVELS[os.environ.get("LOG_LEVEL", "").lower()])
log = logging.getLogger(__name__)


# Retrieve Current Task definition
def retrieve_current_task_def(ecs_client, cluster_name, service_name):
    response = ecs_client.describe_services(
        cluster=cluster_name,
        services=[
            service_name
        ]
    )
    current_services = response['services']
    current_service = current_services[0]
    service_arn = current_service['serviceArn']
    cluster_arn = current_service['clusterArn']
    task_definition_arn = current_service['taskDefinition']
    log.info('current task definition %s ', task_definition_arn)
    return cluster_arn, service_arn, task_definition_arn


# Retrieve Current Image url from current task definition
def retrieve_current_image(ecs_client, task_definition_arn, service):
    response = ecs_client.describe_task_definition(
        taskDefinition=task_definition_arn
    )
    task_definition = response['taskDefinition']
    container_definitions = task_definition['containerDefinitions']
    log.info(container_definitions)
    # required if task definition has multiple containers like sidecars
    container_definition = find_container(container_definitions, service)
    if container_definition is None:
        message = 'Couldnt find any image containing ' + service + ' in current taskDefinition ' + task_definition_arn + '. Aborting '
        raise Exception(message)
    log.info('Current image definition: %s', container_definition['image'])
    return container_definition['image'], task_definition


# Create new task definition using the old task definition and deployment image
def register_new_task_definition(ecs_client, task_definition, current_image, deployment_image):
    container_definitions = task_definition['containerDefinitions']
    # required if task definition has multiple containers like sidecars
    container_definition = find_container(container_definitions, current_image)
    container_definition['image'] = deployment_image
    log.info('New Task def %s', task_definition)
    task_family = task_definition.get('family')
    taskRoleArn = task_definition.get('taskRoleArn')
    executionRoleArn = task_definition.get('executionRoleArn')
    networkMode = task_definition.get('networkMode')
    volumes = task_definition.get('volumes')
    placementConstraints = task_definition.get('placementConstraints')
    requiresCompatibilities = task_definition.get('requiresCompatibilities')
    cpu = task_definition.get('cpu')
    memory = task_definition.get('memory')
    response = ecs_client.register_task_definition(
        family=task_family,
        taskRoleArn=taskRoleArn,
        executionRoleArn=executionRoleArn,
        networkMode=networkMode,
        containerDefinitions=container_definitions,
        volumes=volumes,
        placementConstraints=placementConstraints,
        requiresCompatibilities=requiresCompatibilities,
        cpu=cpu,
        memory=memory
    )
    new_task_def = response['taskDefinition']['taskDefinitionArn']
    log.info('New task definition: %s', new_task_def)
    return new_task_def


# update ecs service using new task definition
def update_service(ecs_client, cluster_arn, service_name, deployment_task_arn):
    response = ecs_client.update_service(
        cluster=cluster_arn,
        service=service_name,
        taskDefinition=deployment_task_arn,
    )
    http_status = response['ResponseMetadata']['HTTPStatusCode']
    if http_status != 200:
        message = 'Unable to update service: ' + service_name + ' with taskDefinition: ' + deployment_task_arn
        raise Exception(message)
    return


def find_container(containers, service):
    for x in containers:
        if service in x['image']:
            return x
    return None


def handler(event, context):
    log.info("Received event: %s", json.dumps(event))
    service = event['service']
    deployment_image = event['image']
    assume_role = event['assumeRole']
    cluster_name = event['clusterName']
    service_name = event['serviceName']

    # Assume Role
    log.info('Assuming role : %s', assume_role)
    assumed_role = sts_client.assume_role(RoleArn=assume_role,
                                          RoleSessionName="AssumeRoleSession1",
                                          DurationSeconds=1800)
    log.info("Successfully assumed role %s", assume_role)

    # Describe Service to retrieve Task Definition
    ecs_client = boto3.client(
        'ecs',
        region_name=REGION,
        aws_access_key_id=assumed_role['Credentials']['AccessKeyId'],
        aws_secret_access_key=assumed_role['Credentials']['SecretAccessKey'],
        aws_session_token=assumed_role['Credentials']['SessionToken']
    )
    # Retrieve Task Definition
    log.info('Retrieving current task definition for service: %s', service_name)
    cluster_arn, service_arn, task_definition_arn = retrieve_current_task_def(ecs_client, cluster_name, service_name)

    # Get Current Image
    log.info('Retrieving current image name for service in taskDefinition: %s', task_definition_arn)
    current_image, current_task_definition = retrieve_current_image(ecs_client, task_definition_arn, service)

    # compare current image with existing image to check if new deployment is needed
    log.info('Comparing %s with %s', current_image, deployment_image)
    if current_image == deployment_image:
        log.info('image matches no new deployment needed')
        event['previousImage'] = current_image
        event['previousTaskDefArn'] = task_definition_arn
        event['deployedTaskDefArn'] = task_definition_arn
        event['deploymentNeeded'] = False
    else:
        log.info('image dos not match new deployment needed')
        log.info('Creating new Task definition using %s', deployment_image)
        deployment_task_arn = register_new_task_definition(ecs_client, current_task_definition, current_image,
                                                           deployment_image)
        log.info('Updating ECS service %s with new task definition %s', service_name, task_definition_arn)
        update_service(ecs_client, cluster_arn, service_name, deployment_task_arn)
        event['previousImage'] = current_image
        event['previousTaskDefArn'] = task_definition_arn
        event['deployedTaskDefArn'] = deployment_task_arn
        event['deploymentNeeded'] = True

    log.info("Received event: %s", json.dumps(event))
    return event
