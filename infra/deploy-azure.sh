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

# Durable agent state in Blob storage, accessed with the managed identity only.
# Set ENABLE_STATE_STORAGE=false to keep state in memory.
ENABLE_STATE_STORAGE="${ENABLE_STATE_STORAGE:-true}"
STATE_STORAGE_ACCOUNT="${STATE_STORAGE_ACCOUNT:-st${REGISTRY_STEM}${SUBSCRIPTION_SUFFIX}}"
STATE_STORAGE_ACCOUNT="$(printf '%s' "$STATE_STORAGE_ACCOUNT" | tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]' | cut -c1-24)"
STATE_STORAGE_CONTAINER="${STATE_STORAGE_CONTAINER:-agent-state}"
# Network Security Perimeter for the state account. Policies that disable public
# network access on storage exempt NSP-associated accounts; the inbound rule lets
# managed identities in this subscription reach it and blocks everything else.
STATE_STORAGE_NSP="${STATE_STORAGE_NSP:-nsp-${AGENT_NAME}}"

AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-}"
AZURE_OPENAI_DEPLOYMENT="${AZURE_OPENAI_DEPLOYMENT:-}"
AZURE_OPENAI_ACCOUNT_ID="${AZURE_OPENAI_ACCOUNT_ID:-}"

# Purview content capture is opt-in; blocking additionally requires a DLP policy.
ENABLE_PURVIEW="${ENABLE_PURVIEW:-false}"
PURVIEW_ENFORCE_BLOCKS="${PURVIEW_ENFORCE_BLOCKS:-false}"

# Email triage is opt-in. Replies and escalations always wait for a Teams approver.
ENABLE_EMAIL_TRIAGE="${ENABLE_EMAIL_TRIAGE:-false}"
EMAIL_TRIAGE_APPROVERS="${EMAIL_TRIAGE_APPROVERS:-}"
EMAIL_TRIAGE_ESCALATION_ADDRESS="${EMAIL_TRIAGE_ESCALATION_ADDRESS:-}"
EMAIL_TRIAGE_INTERNAL_DOMAINS="${EMAIL_TRIAGE_INTERNAL_DOMAINS:-}"
CUSTOMER_WORKBOOK_URL="${CUSTOMER_WORKBOOK_URL:-}"
TRIAGE_ENV=("ENABLE_EMAIL_TRIAGE=$ENABLE_EMAIL_TRIAGE")
if [[ "$ENABLE_EMAIL_TRIAGE" == "true" ]]; then
  [[ -n "$EMAIL_TRIAGE_APPROVERS" ]] || { echo "ERROR: EMAIL_TRIAGE_APPROVERS is required when ENABLE_EMAIL_TRIAGE=true" >&2; exit 1; }
  TRIAGE_ENV+=("EMAIL_TRIAGE_APPROVERS=$EMAIL_TRIAGE_APPROVERS")
  if [[ -n "$EMAIL_TRIAGE_ESCALATION_ADDRESS" ]]; then
    TRIAGE_ENV+=("EMAIL_TRIAGE_ESCALATION_ADDRESS=$EMAIL_TRIAGE_ESCALATION_ADDRESS")
  fi
  if [[ -n "$EMAIL_TRIAGE_INTERNAL_DOMAINS" ]]; then
    TRIAGE_ENV+=("EMAIL_TRIAGE_INTERNAL_DOMAINS=$EMAIL_TRIAGE_INTERNAL_DOMAINS")
  fi
  if [[ -n "$CUSTOMER_WORKBOOK_URL" ]]; then
    TRIAGE_ENV+=("CUSTOMER_WORKBOOK_URL=$CUSTOMER_WORKBOOK_URL")
  fi
fi

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

STATE_ENV=()
if [[ "$ENABLE_STATE_STORAGE" == "true" ]]; then
  echo "==> State storage account $STATE_STORAGE_ACCOUNT"
  az_sub storage account show -g "$RESOURCE_GROUP" -n "$STATE_STORAGE_ACCOUNT" -o none 2>/dev/null ||
    az_sub storage account create -g "$RESOURCE_GROUP" -n "$STATE_STORAGE_ACCOUNT" -l "$LOCATION" \
      --sku Standard_LRS --kind StorageV2 --https-only true --min-tls-version TLS1_2 \
      --allow-blob-public-access false --allow-shared-key-access false -o none
  az_sub storage container-rm show -g "$RESOURCE_GROUP" --storage-account "$STATE_STORAGE_ACCOUNT" \
    -n "$STATE_STORAGE_CONTAINER" -o none 2>/dev/null ||
    az_sub storage container-rm create -g "$RESOURCE_GROUP" --storage-account "$STATE_STORAGE_ACCOUNT" \
      -n "$STATE_STORAGE_CONTAINER" --public-access off -o none
  STATE_STORAGE_ID=$(az_sub storage account show -g "$RESOURCE_GROUP" -n "$STATE_STORAGE_ACCOUNT" --query id -o tsv)
  if [[ -n "$STATE_STORAGE_NSP" ]]; then
    echo "==> Network security perimeter $STATE_STORAGE_NSP"
    az_sub network perimeter show -g "$RESOURCE_GROUP" -n "$STATE_STORAGE_NSP" -o none 2>/dev/null ||
      az_sub network perimeter create -g "$RESOURCE_GROUP" -n "$STATE_STORAGE_NSP" -l "$LOCATION" -o none
    az_sub network perimeter profile show -g "$RESOURCE_GROUP" --perimeter-name "$STATE_STORAGE_NSP" \
      -n agent-state -o none 2>/dev/null ||
      az_sub network perimeter profile create -g "$RESOURCE_GROUP" --perimeter-name "$STATE_STORAGE_NSP" \
        -n agent-state -o none
    az_sub network perimeter profile access-rule show -g "$RESOURCE_GROUP" --perimeter-name "$STATE_STORAGE_NSP" \
      --profile-name agent-state -n allow-subscription-mi -o none 2>/dev/null ||
      az_sub network perimeter profile access-rule create -g "$RESOURCE_GROUP" --perimeter-name "$STATE_STORAGE_NSP" \
        --profile-name agent-state -n allow-subscription-mi --direction Inbound \
        --subscriptions "[{id:/subscriptions/$SUBSCRIPTION_ID}]" -o none
    NSP_PROFILE_ID=$(az_sub network perimeter profile show -g "$RESOURCE_GROUP" \
      --perimeter-name "$STATE_STORAGE_NSP" -n agent-state --query id -o tsv)
    az_sub network perimeter association show -g "$RESOURCE_GROUP" --perimeter-name "$STATE_STORAGE_NSP" \
      -n agent-state-storage -o none 2>/dev/null ||
      az_sub network perimeter association create -g "$RESOURCE_GROUP" --perimeter-name "$STATE_STORAGE_NSP" \
        -n agent-state-storage --access-mode Enforced \
        --private-link-resource "{id:$STATE_STORAGE_ID}" --profile "{id:$NSP_PROFILE_ID}" -o none
  fi
  STATE_BLOB_URL=$(az_sub storage account show -g "$RESOURCE_GROUP" -n "$STATE_STORAGE_ACCOUNT" \
    --query primaryEndpoints.blob -o tsv)
  STATE_ENV=("STATE_STORAGE_BLOB_URL=${STATE_BLOB_URL%/}" "STATE_STORAGE_CONTAINER=$STATE_STORAGE_CONTAINER")
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
      "${TRIAGE_ENV[@]}" \
      ${STATE_ENV[@]+"${STATE_ENV[@]}"} \
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
      "${TRIAGE_ENV[@]}" \
      ${STATE_ENV[@]+"${STATE_ENV[@]}"} \
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
if [[ "$ENABLE_STATE_STORAGE" == "true" ]]; then
  ensure_role "Storage Blob Data Contributor" "$STATE_STORAGE_ID"
fi

FQDN=$(az_sub containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" --query properties.configuration.ingress.fqdn -o tsv)
echo
echo "Deployed: https://$FQDN"
echo "Health  : https://$FQDN/api/health"
echo "Endpoint: https://$FQDN/api/messages   <- messagingEndpoint for a365.config.json"
