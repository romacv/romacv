import base64
import json
import os
import urllib.request
from google.api_core import exceptions
from google.cloud import billing_v1, logging
from firebase_functions import pubsub_fn

SIMULATE_DEACTIVATION = True


def get_project_id():
    project = os.getenv('GOOGLE_CLOUD_PROJECT')
    if not project:
        request = urllib.request.Request('http://metadata.google.internal/computeMetadata/v1/project/project-id',headers={'Metadata-Flavor':'Google'})
        project = urllib.request.urlopen(request,timeout=10).read().decode()
    if not project:
        raise ValueError('project-id metadata not found')
    return project


def stop_billing(cloud_event):
    event = json.loads(base64.b64decode(cloud_event.data.message.data))
    if event['costAmount'] <= event['budgetAmount']:
        return
    billing_client = billing_v1.CloudBillingClient()
    project = 'projects/' + get_project_id()
    if _is_billing_enabled(project, billing_client):
        _disable_billing_for_project(project,SIMULATE_DEACTIVATION,billing_client)


def _is_billing_enabled(project_name, billing_client):
    try:
        return billing_client.get_project_billing_info(name=project_name).billing_enabled
    except Exception:
        return True


def _disable_billing_for_project(project_name, simulate_deactivation, billing_client):
    logger = logging.Client().logger('disable-billing')
    if simulate_deactivation:
        logger.log_text('Billing disabled. (Simulated)',severity='CRITICAL')
        return
    try:
        billing_client.update_project_billing_info(name=project_name,project_billing_info=billing_v1.ProjectBillingInfo(billing_account_name=''))
        logger.log_text('Billing disabled',severity='CRITICAL')
    except exceptions.PermissionDenied:
        logger.log_text('Failed to disable billing, check permissions',severity='ERROR')


@pubsub_fn.on_message_published(topic='budget-alerts',region='us-central1',service_account='autopost-budget@resromcom-493b4.iam.gserviceaccount.com',retry=False)
def budget_kill(event):
    stop_billing(event)
