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


# Retrieve Current Tasks
def retrieve_current_tasks(ecs_client, cluster_name, service_name):
    response = ecs_client.list_tasks(
        cluster=cluster_name,
        maxResults=100,
        serviceName=service_name,
        desiredStatus='RUNNING'
    )
    running_tasks = response['taskArns']
    log.info('Currently running tasks %s', running_tasks)
    return running_tasks


# validate if all the running tasks are using new deployed_task_arn This is based on the fact that ECS will only kill
# older tasks if the new tasks are RUNNING and passing health check with ROLLING DEPLOYMENTS,
# This is a fail safe mechanism whereby ECS prevents outage by deploying unhealthy tasks
def validate_running_tasks(ecs_client, cluster_name, running_tasks, service_name, deployed_task_arn):
    response = ecs_client.describe_tasks(
        cluster=cluster_name,
        tasks=running_tasks,
    )
    running_tasks_desc = response['tasks']
    for task in running_tasks_desc:
        task_arn = task['taskDefinitionArn']
        if task_arn != deployed_task_arn:
            message = 'Found older task definition: ' + task_arn + ' still deployed in ecs service: ' + service_name + '. Failing Task. Task will be retried 3 times with exponential delay before marking deployment failed '
            raise Exception(message)
    return


def handler(event, context):
    log.info("Received event: %s", json.dumps(event))
    deployment_image = event['image']
    assume_role = event['assumeRole']
    cluster_name = event['clusterName']
    service_name = event['serviceName']
    deployed_task_arn = event['deployedTaskDefArn']
    deployment_needed = event['deploymentNeeded']
    del event['deploymentNeeded']

    # No deployment needed, not validation required
    if not deployment_needed:
        log.info('No deployment needed, not validation required, Marking task successful')
        event['deployed'] = False
    else:
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

        # retrieve currently running tasks
        log.info('Retrieving Currently running tasks for service: %s ', service_name)
        running_tasks = retrieve_current_tasks(ecs_client, cluster_name, service_name)
        if len(running_tasks) == 0:
            log.warning(
                'No currently running rasks found, assuming service descaled to 0, adding warning, marking deployment successful')
            event['warning'] = 'No running task found for service :' + service_name
        else:
            # Validate if the all the RUNNING tasks are from new task arn
            log.info(
                'Validating currently running tasks to check if all the tasks are with the new task definition arn')
            validate_running_tasks(ecs_client, cluster_name, running_tasks, service_name, deployed_task_arn)
            log.info('All running tasks are with new Task Def Arn, Marking deployment successful')
            event['deployed'] = True

    return event
