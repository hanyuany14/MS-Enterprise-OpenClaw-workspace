from fabric_data_agent_client import FabricDataAgentClient
from diagnose_sql_location import diagnose
import os
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())

obo_token = os.environ["FABRIC_ACCESS_TOKEN"]
client = FabricDataAgentClient(
    tenant_id=os.environ["AZURE_TENANT_ID"],
    data_agent_url=os.environ["DATA_AGENT_URL"],
    token=obo_token,
)
diagnose(client, "retrieve the last two months of weekly Orders growth rate results from the backend database")