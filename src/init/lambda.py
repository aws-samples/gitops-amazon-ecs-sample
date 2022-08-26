import boto3
import collections
import json
import logging
import os


DEFAULT_LOG_LEVEL = logging.DEBUG
REGION = os.environ['REGION']
ACCOUNT_ID = os.environ['ACCOUNT_ID']
ECS_DEPLOYMENT_ROLE_ARN = os.environ['ECS_DEPLOYMENT_ROLE_ARN']

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


def handler(event, context):
    log.info("Received event: %s", json.dumps(event))
    release = event['release']
    log.info(f'Processing items release: {release}')
    tasks = event['tasks']
    services = event['services']
    if len(services) == 0:
        message = 'No services found for deployment'
        raise Exception(message)
    for service in services:
        service['assumeRole'] = ECS_DEPLOYMENT_ROLE_ARN
    if tasks:
        for task in tasks:
            task['assumeRole'] = ECS_DEPLOYMENT_ROLE_ARN
    return event
