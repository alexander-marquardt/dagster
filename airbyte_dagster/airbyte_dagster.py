from dagster import job
from dagster_airbyte import airbyte_resource, airbyte_sync_op
import os

API_AUTH_TOKEN = os.environ.get('API_AUTH_TOKEN')
my_airbyte_resource = airbyte_resource.configured(
   {
       "host": "cloud.airbyte.com",
       "port":  "443",
       "use_https": True,
       "api_auth_token":  API_AUTH_TOKEN
   }
)
sync_foobar = airbyte_sync_op.configured({"connection_id": "b14e9a40-c4c5-42fd-9894-708fa325f38c"}, name="sync_foobar")

@job(resource_defs={"airbyte": my_airbyte_resource})
def my_simple_airbyte_job():
    sync_foobar()
