# Azure infrastructure

Bicep for one Capium Time & Fees environment per resource group. Region: `uksouth` by default
(the resource group's location).

```text
Browser ──HTTPS──► ui (Container App, nginx) ──/api──► api (internal ingress) ──TLS──► MySQL Flexible Server
                                                        worker (no ingress) ───────┘   (private subnet)
                      api, worker ──managed identity──► Blob Storage (uploads), Key Vault (secrets)
```

| Module | Resources |
|---|---|
| `network.bicep` | Virtual network: `snet-apps` (/23, Container Apps), `snet-mysql` (/24, delegated to MySQL) |
| `monitoring.bicep` | Log Analytics workspace (container logs, 30 days) |
| `identity.bicep` | User-assigned managed identity shared by every app and job |
| `registry.bicep` | Container Registry (`capium-api`, `capium-worker`, `capium-ui`), AcrPull for the identity |
| `mysql.bicep` | MySQL 8.4 Flexible Server in the private subnet + private DNS zone, TLS required, `READ-COMMITTED`, database `capium` (`utf8mb4_0900_as_cs`) |
| `storage.bicep` | Storage account (no public blobs, no shared keys, 14-day soft delete), private container `uploads`, *Storage Blob Data Contributor* for the identity |
| `keyvault.bicep` | Key Vault (RBAC): `db-url`, `jwt-secret-key`, `mfa-encryption-key`, `smtp-password` (when set), *Key Vault Secrets User* for the identity |
| `apps.bicep` | Container Apps environment (VNet), apps `api`, `worker`, `ui`, manual job `migrate` |
| `email.bicep` | Optional (`deployEmail=true`): Communication Services Email with an Azure-managed domain |

Names are `<type>-capium-<env>[-<suffix>]`; the registry, vault, database server and storage
account carry a 6-character suffix derived from the resource group, since they must be globally unique.

## Prerequisites

* Azure CLI 2.60+ (`az bicep install`), signed in with **Owner** (or Contributor + User Access
  Administrator) on the resource group: the templates create role assignments.
* Docker is not needed: images are built in the registry with `az acr build`.

## First deployment of an environment

```bash
export MYSQL_ADMIN_PASSWORD='<12+ characters>'
export JWT_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export MFA_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
az group create -n rg-capium-dev -l uksouth

# 1. Everything except the apps (the images do not exist yet).
DEPLOY_APPS=false az deployment group create -g rg-capium-dev -f infra/main.bicep -p infra/env/dev.bicepparam
ACR=$(az acr list -g rg-capium-dev --query "[0].name" -o tsv)

# 2. Images: API and worker from this repository, UI from capiumtimenfeeui.
TAG=$(git rev-parse HEAD)
az acr build -r "$ACR" -t capium-api:$TAG --target api .
az acr build -r "$ACR" -t capium-worker:$TAG --target worker .
(cd ../capiumtimenfeeui && az acr build -r "$ACR" -t capium-ui:$TAG .)

# 3. The apps and the migration job.
API_IMAGE_TAG=$TAG UI_IMAGE_TAG=$TAG az deployment group create -g rg-capium-dev -f infra/main.bicep -p infra/env/dev.bicepparam

# 4. Schema, then the first practice and its Owner (prints a temporary password once).
az containerapp job start -n capium-dev-migrate -g rg-capium-dev
az containerapp job execution list -n capium-dev-migrate -g rg-capium-dev -o table   # wait for Succeeded
az containerapp exec -n capium-dev-api -g rg-capium-dev --command \
  "python -m app.maintenance.bootstrap --practice-name 'Smith & Co' --admin-name 'Ada Smith' --admin-email ada@smithco.example"
```

Keep `MYSQL_ADMIN_PASSWORD`, `JWT_SECRET_KEY` and `MFA_ENCRYPTION_KEY` for later deployments
(they are in the vault): deploying with a different value rotates them. Rotating
`MFA_ENCRYPTION_KEY` makes every stored two-factor secret unreadable, so those people need an
administrator to reset their two-factor sign-in.

The `deployApps=false` → `true` sequence is only needed once. Role assignments can take a few
minutes to apply; if step 3 fails pulling an image or reading a secret, run it again.

## Releases

Use the **Deploy to Azure** workflow (`.github/workflows/deploy-azure.yml`, manual trigger). It
builds both images in the registry, runs `migrate` and waits for it, rolls out `api` and `worker`,
then checks that `/api/v1/system` answers 200 through the UI. Its one-off setup: a GitHub environment
per target (`dev`, `prod`) with the variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
`AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, and an Entra application with a federated
credential for `repo:<org>/capiumtimenfeeapi:environment:<env>` holding *Contributor* on the
resource group and *AcrPush* on the registry.

The UI is released from its own repository: `az acr build -r <acr> -t capium-ui:<tag> .`, then
`az containerapp update -n capium-<env>-ui -g <rg> --image <acr>.azurecr.io/capium-ui:<tag>`.
The API goes first when a release has migrations (CLAUDE.md).

MySQL DDL is not transactional: a migration that fails part-way leaves its earlier statements
applied. Before a release with migrations, take an on-demand backup
(`az mysql flexible-server backup create`), and restore it if `migrate` fails.

## Production notes

* `prod.bicepparam`: General Purpose MySQL, 35 days of point-in-time restore, geo-redundant
  backups, GZRS storage, vault purge protection (irreversible).
* Custom domain: bind it to the `ui` app (`az containerapp hostname add` + managed certificate),
  then deploy with `FRONTEND_URL=https://<domain>` so links in emails, cookies and `CORS_ORIGINS` use it.
* The API connects as the MySQL administrator. For least privilege, create a user with rights on
  `capium` only, store its URL in the vault secret `db-url`, and restart the revisions.
* Email: without `SMTP_HOST` the API records messages instead of sending them. With
  `deployEmail=true`, finish the setup in the portal (an Entra application as the SMTP username
  of the Communication Services resource), then set `SMTP_HOST=smtp.azurecomm.net`, `SMTP_USER`,
  `SMTP_PASSWORD` and `SMTP_FROM_ADDRESS=DoNotReply@<sender domain>`. Any other SMTP relay works the same way.
* Logs: Log Analytics → `ContainerAppConsoleLogs_CL`, filtered by `ContainerAppName_s`.

## Validation

CI compiles and lints the templates (`az bicep build`, `az bicep lint`, `az bicep build-params`).
Before the first real deployment, preview it:
`az deployment group what-if -g <rg> -f infra/main.bicep -p infra/env/<env>.bicepparam`.
