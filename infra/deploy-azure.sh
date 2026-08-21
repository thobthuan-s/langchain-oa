#!/usr/bin/env bash
# Provision Azure infrastructure for LangchainOA and deploy the container image.
# Idempotent — safe to re-run. Creates nothing outside the target resource group.
set -euo pipefail

AGENT_NAME="${AGENT_NAME:-langchainoa}"
LOCATION="${LOCATION:-}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-agent365-${AGENT_NAME}-${LOCATION}}"
APP_NAME="${APP_NAME:-${AGENT_NAME}}"
ENVIRONMENT_NAME="${ENVIRONMENT_NAME:-cae-${AGENT_NAME}}"
WORKSPACE_NAME="${WORKSPACE_NAME:-law-${AGENT_NAME}}"
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-$(az account show --query id -o tsv)}"
SUBSCRIPTION_SUFFIX="$(printf '%s' "$SUBSCRIPTION_ID" | tr -cd '[:alnum:]' | cut -c1-6)"
REGISTRY_STEM="$(printf '%s' "$AGENT_NAME" | tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]')"
REGISTRY_NAME="${REGISTRY_NAME:-acr${REGISTRY_STEM}${SUBSCRIPTION_SUFFIX}}"

# ACR Tasks (server-side `az acr build`) is not offered in every region. When the
# app region lacks it, set ACR_LOCATION to a supported one — the registry region
# does not have to match the container app region.
ACR_LOCATION="${ACR_LOCATION:-$LOCATION}"

AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-}"
AZURE_OPENAI_DEPLOYMENT="${AZURE_OPENAI_DEPLOYMENT:-}"
AZURE_OPENAI_ACCOUNT_ID="${AZURE_OPENAI_ACCOUNT_ID:-}"

# Purview content capture is opt-in; blocking additionally requires a DLP policy.
ENABLE_PURVIEW="${ENABLE_PURVIEW:-false}"
PURVIEW_ENFORCE_BLOCKS="${PURVIEW_ENFORCE_BLOCKS:-false}"

require() { [[ -n "${2:-}" ]] || { echo "ERROR: $1 is required" >&2; exit 1; }; }
require LOCATION "$LOCATION"
require AZURE_OPENAI_ENDPOINT "$AZURE_OPENAI_ENDPOINT"
require AZURE_OPENAI_DEPLOYMENT "$AZURE_OPENAI_DEPLOYMENT"

az_sub() { az "$@" --subscription "$SUBSCRIPTION_ID"; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "==> Subscription : $SUBSCRIPTION_ID"
echo "==> Resource group: $RESOURCE_GROUP ($LOCATION)"
echo "==> Registry      : $REGISTRY_NAME ($ACR_LOCATION)"

echo "==> Resource group"
az_sub group create -n "$RESOURCE_GROUP" -l "$LOCATION" -o none

echo "==> Container registry"
az_sub acr show -g "$RESOURCE_GROUP" -n "$REGISTRY_NAME" -o none 2>/dev/null ||
  az_sub acr create -g "$RESOURCE_GROUP" -n "$REGISTRY_NAME" -l "$ACR_LOCATION" \
    --sku Basic --admin-enabled false -o none

echo "==> Log Analytics workspace"
az_sub monitor log-analytics workspace show -g "$RESOURCE_GROUP" -n "$WORKSPACE_NAME" -o none 2>/dev/null ||
  az_sub monitor log-analytics workspace create -g "$RESOURCE_GROUP" -n "$WORKSPACE_NAME" -l "$LOCATION" -o none

echo "==> Container Apps environment"
if ! az_sub containerapp env show -g "$RESOURCE_GROUP" -n "$ENVIRONMENT_NAME" -o none 2>/dev/null; then
  WORKSPACE_ID=$(az_sub monitor log-analytics workspace show -g "$RESOURCE_GROUP" -n "$WORKSPACE_NAME" --query customerId -o tsv)
  WORKSPACE_KEY=$(az_sub monitor log-analytics workspace get-shared-keys -g "$RESOURCE_GROUP" -n "$WORKSPACE_NAME" --query primarySharedKey -o tsv)
  az_sub containerapp env create -g "$RESOURCE_GROUP" -n "$ENVIRONMENT_NAME" -l "$LOCATION" \
    --logs-workspace-id "$WORKSPACE_ID" --logs-workspace-key "$WORKSPACE_KEY" -o none
fi

# A unique tag per build: updating an unchanged :latest tag does not create a new revision.
TAG="$(date +%Y%m%d%H%M%S)"
IMAGE="${REGISTRY_NAME}.azurecr.io/${AGENT_NAME}:${TAG}"

echo "==> Building image $IMAGE"
az_sub acr build --registry "$REGISTRY_NAME" --image "${AGENT_NAME}:${TAG}" \
  --file "$PROJECT_DIR/Dockerfile" "$PROJECT_DIR" -o none

if az_sub containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" -o none 2>/dev/null; then
  echo "==> Updating container app"
  az_sub containerapp update -g "$RESOURCE_GROUP" -n "$APP_NAME" --image "$IMAGE" \
    --set-env-vars \
      "AZURE_OPENAI_ENDPOINT=$AZURE_OPENAI_ENDPOINT" \
      "AZURE_OPENAI_DEPLOYMENT=$AZURE_OPENAI_DEPLOYMENT" \
      "AZURE_SUBSCRIPTION_ID=$SUBSCRIPTION_ID" \
      "PYTHON_ENVIRONMENT=Production" \
      "ENABLE_LOCAL_EVAL=false" \
      "ENABLE_WORKIQ=true" \
      "ENABLE_A365_OBSERVABILITY=true" \
      "ENABLE_A365_OBSERVABILITY_EXPORTER=true" \
      "ENABLE_PURVIEW=$ENABLE_PURVIEW" \
      "PURVIEW_ENFORCE_BLOCKS=$PURVIEW_ENFORCE_BLOCKS" \
      "LOG_LEVEL=INFO" \
    -o none
else
  echo "==> Creating container app"
  az_sub containerapp create -g "$RESOURCE_GROUP" -n "$APP_NAME" \
    --environment "$ENVIRONMENT_NAME" --image "$IMAGE" \
    --registry-server "${REGISTRY_NAME}.azurecr.io" --registry-identity system \
    --system-assigned --target-port 8080 --ingress external \
    --min-replicas 1 --max-replicas 1 --cpu 1.0 --memory 2.0Gi \
    --env-vars \
      "AZURE_OPENAI_ENDPOINT=$AZURE_OPENAI_ENDPOINT" \
      "AZURE_OPENAI_DEPLOYMENT=$AZURE_OPENAI_DEPLOYMENT" \
      "AZURE_SUBSCRIPTION_ID=$SUBSCRIPTION_ID" \
      "PYTHON_ENVIRONMENT=Production" \
      "ENABLE_LOCAL_EVAL=false" \
      "ENABLE_WORKIQ=true" \
      "ENABLE_A365_OBSERVABILITY=true" \
      "ENABLE_A365_OBSERVABILITY_EXPORTER=true" \
      "ENABLE_PURVIEW=$ENABLE_PURVIEW" \
      "PURVIEW_ENFORCE_BLOCKS=$PURVIEW_ENFORCE_BLOCKS" \
      "LOG_LEVEL=INFO" \
    -o none
fi

PRINCIPAL_ID=$(az_sub containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" --query identity.principalId -o tsv)
echo "==> Managed identity: $PRINCIPAL_ID"

echo "==> Role assignments"
ensure_role() {
  local role="$1"
  local scope="$2"
  if [[ "$(az role assignment list --assignee-object-id "$PRINCIPAL_ID" --scope "$scope" --role "$role" --query 'length(@)' -o tsv)" == "0" ]]; then
    az role assignment create --assignee-object-id "$PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
      --role "$role" --scope "$scope" -o none
  else
    echo "    ($role already assigned)"
  fi
}
if [[ -n "$AZURE_OPENAI_ACCOUNT_ID" ]]; then
  ensure_role "Cognitive Services OpenAI User" "$AZURE_OPENAI_ACCOUNT_ID"
else
  echo "    SKIPPED: set AZURE_OPENAI_ACCOUNT_ID to grant model access automatically."
fi
ensure_role "Reader" "/subscriptions/$SUBSCRIPTION_ID"

FQDN=$(az_sub containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" --query properties.configuration.ingress.fqdn -o tsv)
echo
echo "Deployed: https://$FQDN"
echo "Health  : https://$FQDN/api/health"
echo "Endpoint: https://$FQDN/api/messages   <- messagingEndpoint for a365.config.json"
