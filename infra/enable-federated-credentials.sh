#!/usr/bin/env bash
# Switch the Blueprint credential from a client secret to a federated identity
# credential backed by a user-assigned managed identity. Idempotent.
#
# 1. Creates (or reuses) a user-assigned managed identity and attaches it to the
#    Container App alongside the existing system-assigned identity.
# 2. Adds a federated identity credential on the Blueprint app that trusts it.
# 3. Sets AUTHTYPE=FederatedCredentials and removes the secret from the app.
#
# The old Blueprint secret is NOT deleted here. Verify a live turn first, then
# remove it from Entra (see docs/deploy-in-your-tenant.md).
set -euo pipefail

AGENT_NAME="${AGENT_NAME:-langchainoa}"
LOCATION="${LOCATION:-}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-agent365-${AGENT_NAME}-${LOCATION}}"
APP_NAME="${APP_NAME:-${AGENT_NAME}}"
IDENTITY_NAME="${IDENTITY_NAME:-id-${APP_NAME}}"
FIC_NAME="${FIC_NAME:-${APP_NAME}-container}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-$(az account show --query id -o tsv)}"

[[ -n "$LOCATION" ]] || { echo "ERROR: LOCATION is required" >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "ERROR: $ENV_FILE not found" >&2; exit 1; }

read_value() {
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -1
}
BLUEPRINT_APP_ID="$(read_value CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID)"
TENANT_ID="$(read_value CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID)"
[[ -n "$BLUEPRINT_APP_ID" && -n "$TENANT_ID" ]] || { echo "ERROR: Blueprint CLIENTID/TENANTID missing in $ENV_FILE" >&2; exit 1; }

az_sub() { az "$@" --subscription "$SUBSCRIPTION_ID"; }

echo "==> User-assigned managed identity: $IDENTITY_NAME"
if ! az_sub identity show -g "$RESOURCE_GROUP" -n "$IDENTITY_NAME" -o none 2>/dev/null; then
  az_sub identity create -g "$RESOURCE_GROUP" -n "$IDENTITY_NAME" -l "$LOCATION" -o none
fi
IDENTITY_ID="$(az_sub identity show -g "$RESOURCE_GROUP" -n "$IDENTITY_NAME" --query id -o tsv)"
IDENTITY_CLIENT_ID="$(az_sub identity show -g "$RESOURCE_GROUP" -n "$IDENTITY_NAME" --query clientId -o tsv)"
IDENTITY_PRINCIPAL_ID="$(az_sub identity show -g "$RESOURCE_GROUP" -n "$IDENTITY_NAME" --query principalId -o tsv)"

echo "==> Attaching identity to $APP_NAME (system-assigned identity is kept)"
az_sub containerapp identity assign -g "$RESOURCE_GROUP" -n "$APP_NAME" --user-assigned "$IDENTITY_ID" -o none

echo "==> Federated credential on Blueprint $BLUEPRINT_APP_ID"
existing="$(az ad app federated-credential list --id "$BLUEPRINT_APP_ID" --query "[?subject=='$IDENTITY_PRINCIPAL_ID'] | length(@)" -o tsv)"
if [[ "$existing" == "0" ]]; then
  az ad app federated-credential create --id "$BLUEPRINT_APP_ID" -o none --parameters "{
    \"name\": \"$FIC_NAME\",
    \"issuer\": \"https://login.microsoftonline.com/$TENANT_ID/v2.0\",
    \"subject\": \"$IDENTITY_PRINCIPAL_ID\",
    \"audiences\": [\"api://AzureADTokenExchange\"],
    \"description\": \"Container App $APP_NAME via managed identity $IDENTITY_NAME\"
  }"
else
  echo "    (already present)"
fi

echo "==> Switching the app to FederatedCredentials"
az_sub containerapp update -g "$RESOURCE_GROUP" -n "$APP_NAME" -o none \
  --set-env-vars \
    "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE=FederatedCredentials" \
    "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__FEDERATEDCLIENTID=$IDENTITY_CLIENT_ID" \
  --remove-env-vars "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET" "CLIENT_SECRET"

if az_sub containerapp secret list -g "$RESOURCE_GROUP" -n "$APP_NAME" --query "[?name=='service-connection-secret'] | length(@)" -o tsv | grep -q '^1$'; then
  az_sub containerapp secret remove -g "$RESOURCE_GROUP" -n "$APP_NAME" --secret-names service-connection-secret -o none
fi

echo
echo "Done. Blueprint auth is now keyless."
echo "  FEDERATEDCLIENTID = $IDENTITY_CLIENT_ID"
echo "Next: send the agent a Teams message, confirm it replies, then delete the"
echo "old Blueprint secret:  az ad app credential list --id $BLUEPRINT_APP_ID"
