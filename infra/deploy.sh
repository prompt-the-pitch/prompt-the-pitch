#!/usr/bin/env bash
# Deploy Prompt the Pitch to Azure Container Apps.
#
# Builds the image, pushes it to Azure Container Registry, runs it on Container Apps with a
# system-assigned managed identity, and lets that identity call the Foundry model (no API key
# anywhere). Safe to re-run: existing resources are reused and the image is updated.
#
#   ./infra/deploy.sh                 # builds locally with Docker (e.g. Colima) when available
#   BUILD=acr ./infra/deploy.sh       # builds in Azure with ACR Tasks (not allowed on trial subscriptions)
#
# Configuration comes from the environment or .env (FOUNDRY_PROJECT_ENDPOINT, FOUNDRY_MODEL_DEPLOYMENT).
set -euo pipefail
export PYTHONWARNINGS=ignore  # Silences Azure CLI's own Python warnings.

cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a

RG=${RG:-rg-insidethegame}
LOCATION=${LOCATION:-swedencentral}
FOUNDRY_ACCOUNT=${FOUNDRY_ACCOUNT:-itg-foundry-ag26}
SUFFIX=$(az account show --query id -o tsv | tr -d '-' | cut -c1-8)
ACR=${ACR:-itgacr$SUFFIX}
ENV_NAME=${ENV_NAME:-itg-env}
APP=${APP:-prompt-the-pitch}
TAG=${TAG:-$(date +%Y%m%d-%H%M%S)}
IMAGE="$ACR.azurecr.io/prompt-the-pitch:$TAG"
FOUNDRY_USER_ROLE=53ca6127-db72-4b80-b1b0-d745d6d5456d  # "Foundry User" (formerly Azure AI User)

: "${FOUNDRY_PROJECT_ENDPOINT:?Set FOUNDRY_PROJECT_ENDPOINT (see .env)}"
: "${FOUNDRY_MODEL_DEPLOYMENT:?Set FOUNDRY_MODEL_DEPLOYMENT (see .env)}"

exists() { "$@" >/dev/null 2>&1; }

echo "==> Container registry $ACR"
exists az acr show -n "$ACR" -g "$RG" ||
  az acr create -n "$ACR" -g "$RG" -l "$LOCATION" --sku Basic --admin-enabled false -o none

BUILD=${BUILD:-$(docker info >/dev/null 2>&1 && echo local || echo acr)}
if [ "$BUILD" = local ]; then
  echo "==> Building $IMAGE locally (linux/amd64) and pushing it"
  az acr login -n "$ACR" >/dev/null
  docker buildx build --platform linux/amd64 -t "$IMAGE" --push .
else
  echo "==> Building $IMAGE in Azure"
  az acr build -r "$ACR" -t "prompt-the-pitch:$TAG" --no-logs -o none .
fi

echo "==> Container Apps environment $ENV_NAME"
exists az containerapp env show -n "$ENV_NAME" -g "$RG" ||
  az containerapp env create -n "$ENV_NAME" -g "$RG" -l "$LOCATION" -o none

ENV_VARS=(
  "FOUNDRY_PROJECT_ENDPOINT=$FOUNDRY_PROJECT_ENDPOINT"
  "FOUNDRY_MODEL_DEPLOYMENT=$FOUNDRY_MODEL_DEPLOYMENT"
  "ITG_MAX_LIVE_SESSIONS=${ITG_MAX_LIVE_SESSIONS:-3}"
)

echo "==> Container app $APP"
if ! exists az containerapp show -n "$APP" -g "$RG"; then
  # Created with a public placeholder image: the identity must exist before it can pull from ACR.
  az containerapp create -n "$APP" -g "$RG" --environment "$ENV_NAME" \
    --image mcr.microsoft.com/k8se/quickstart:latest --system-assigned \
    --ingress external --target-port 8000 \
    --cpu 1.0 --memory 2Gi --min-replicas 1 --max-replicas 1 -o none
fi

echo "==> Granting the app's managed identity: AcrPull on the registry, Foundry User on Foundry"
PRINCIPAL=$(az containerapp show -n "$APP" -g "$RG" --query identity.principalId -o tsv)
ACR_ID=$(az acr show -n "$ACR" -g "$RG" --query id -o tsv)
FOUNDRY_ID=$(az cognitiveservices account show -n "$FOUNDRY_ACCOUNT" -g "$RG" --query id -o tsv)
for grant in "AcrPull $ACR_ID" "$FOUNDRY_USER_ROLE $FOUNDRY_ID"; do
  set -- $grant
  exists az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal \
    --role "$1" --scope "$2" || true
done

echo "==> Connecting the registry (waiting for the role to propagate)"
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  az containerapp registry set -n "$APP" -g "$RG" --server "$ACR.azurecr.io" --identity system -o none 2>/dev/null && break
  [ "$attempt" = 10 ] && { echo "Could not connect the registry" >&2; exit 1; }
  sleep 20
done

echo "==> Deploying $IMAGE"
# The CLI sometimes reports an InternalServerError although the revision is created; judge by the result.
# One replica: watch rooms live in memory. 1 vCPU: win probabilities are simulated on the fly.
az containerapp update -n "$APP" -g "$RG" --image "$IMAGE" --cpu 1.0 --memory 2Gi \
  --set-env-vars "${ENV_VARS[@]}" -o none ||
  echo "    (the CLI reported an error; checking the revision itself)"
for attempt in $(seq 1 30); do
  STATUS=$(az containerapp revision list -n "$APP" -g "$RG" \
    --query "[?properties.template.containers[0].image=='$IMAGE'] | [0].properties.healthState" -o tsv)
  [ "$STATUS" = Healthy ] && break
  [ "$attempt" = 30 ] && { echo "The new revision is not healthy: ${STATUS:-not found}" >&2; exit 1; }
  sleep 10
done

URL="https://$(az containerapp show -n "$APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
echo
echo "Deployed: $URL"
echo "Health:   $URL/api/health"
