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
def retrieve_task_definition(events_client, cw_rule_name):
    response = events_client.list_targets_by_rule(
        Rule=cw_rule_name,
        Limit=1
    )
    targets = response['Targets']
    target = targets[0]
    task_definition_arn = target['EcsParameters']['TaskDefinitionArn']
    log.info('current task definition %s ', task_definition_arn)
    return task_definition_arn, target


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
        message = 'Couldnt find any image containing ' + service + ' in current taskDefinition ' \
                  + task_definition_arn + '. Aborting '
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
    response = ecs_client.register_task_definition(
        family=task_definition['family'],
        taskRoleArn=task_definition['taskRoleArn'],
        executionRoleArn=task_definition['executionRoleArn'],
        networkMode=task_definition['networkMode'],
        containerDefinitions=container_definitions,
        volumes=task_definition['volumes'],
        placementConstraints=task_definition['placementConstraints'],
        requiresCompatibilities=task_definition['requiresCompatibilities'],
        cpu=task_definition['cpu'],
        memory=task_definition['memory']
    )
    new_task_def = response['taskDefinition']['taskDefinitionArn']
    log.info('New task definition: %s', new_task_def)
    return new_task_def


# update cw rules target with the new task definition
def update_cw_rule_target(events_client, cw_rule_name, target, new_deployment_task_arn):
    target['EcsParameters']['TaskDefinitionArn'] = new_deployment_task_arn
    response = events_client.put_targets(
        Rule=cw_rule_name,
        Targets=[
            target
        ]
    )
    log.info('Update Response: %s', response)
    http_status = response['ResponseMetadata']['HTTPStatusCode']
    if http_status != 200:
        message = 'Unable to update target for cw rule: ' + cw_rule_name
        raise Exception(message)
    return


def find_container(containers, service):
    for x in containers:
        if service in x['image']:
            return x
    # sometimes tasks have different name but are using a same image, so service name doesn't match the image
    # in this scenario return the first container definition
    if len(containers) > 0:
        return containers[0]
    return None


def handler(event, context):
    log.info("Received event: %s", json.dumps(event))
    service = event['service']
    deployment_image = event['image']
    assume_role = event['assumeRole']
    cw_rule_name = event['cwRuleName']

    # Assume Role
    log.info('Assuming role : %s', assume_role)
    assumed_role = sts_client.assume_role(RoleArn=assume_role,
                                          RoleSessionName="AssumeRoleSession1",
                                          DurationSeconds=1800)
    log.info("Successfully assumed role %s", assume_role)

    events_client = boto3.client(
        'events',
        region_name=REGION,
        aws_access_key_id=assumed_role['Credentials']['AccessKeyId'],
        aws_secret_access_key=assumed_role['Credentials']['SecretAccessKey'],
        aws_session_token=assumed_role['Credentials']['SessionToken']
    )

    # Describe Service to retrieve Task Definition
    ecs_client = boto3.client(
        'ecs',
        region_name=REGION,
        aws_access_key_id=assumed_role['Credentials']['AccessKeyId'],
        aws_secret_access_key=assumed_role['Credentials']['SecretAccessKey'],
        aws_session_token=assumed_role['Credentials']['SessionToken']
    )

    # Retrieve Task Definition
    log.info('Finding target of rule of scheduled task cloudwatch rule of %s', cw_rule_name)
    current_task_definition_arn, cw_target = retrieve_task_definition(events_client, cw_rule_name)

    # Get Current Image
    log.info('Retrieving current image name for service in taskDefinition: %s', current_task_definition_arn)
    current_image, current_task_definition = retrieve_current_image(ecs_client, current_task_definition_arn,
                                                                    service)

    # compare current image with existing image to check if new deployment is needed
    log.info('Comparing %s with %s', current_image, deployment_image)
    if current_image == deployment_image:
        log.info('image matches no new deployment needed')
        event['previousImage'] = current_image
        event['previousTaskDefArn'] = current_task_definition_arn
        event['deployedTaskDefArn'] = current_task_definition_arn
        event['deploymentNeeded'] = False
    else:
        log.info('image dos not match new deployment needed')
        log.info('Creating new Task definition using %s', deployment_image)
        new_deployment_task_arn = register_new_task_definition(ecs_client, current_task_definition, current_image,
                                                               deployment_image)
        log.info('Updating Cloudwatch event target of %s with new task definition %s', cw_rule_name,
                 new_deployment_task_arn)
        update_cw_rule_target(events_client, cw_rule_name, cw_target, new_deployment_task_arn)
        event['previousImage'] = current_image
        event['previousTaskDefArn'] = current_task_definition_arn
        event['deployedTaskDefArn'] = new_deployment_task_arn
        event['deploymentNeeded'] = True

    log.info("Output: %s", json.dumps(event))
    return event
