"""Check that the Foundry project answers, using the `az login` credentials."""

import os

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv()

project = AIProjectClient(
    endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
    credential=DefaultAzureCredential(),
)

with project.get_openai_client() as client:
    response = client.responses.create(
        model=os.environ["FOUNDRY_MODEL_DEPLOYMENT"],
        input="Commente en une phrase un but à la 89e minute.",
    )
    print(response.output_text)
