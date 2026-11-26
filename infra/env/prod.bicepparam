// Production environment. Secrets come from the shell (or the deploy workflow), never from this file.
//   az deployment group create -g rg-capium-prod -f infra/main.bicep -p infra/env/prod.bicepparam
using '../main.bicep'

param environmentName = 'prod'
param deployApps = bool(readEnvironmentVariable('DEPLOY_APPS', 'true'))
param apiImageTag = readEnvironmentVariable('API_IMAGE_TAG', 'latest')
param uiImageTag = readEnvironmentVariable('UI_IMAGE_TAG', 'latest')
// The practice-facing address once a custom domain is bound to the UI app; empty until then.
param frontendUrl = readEnvironmentVariable('FRONTEND_URL', '')

param mysqlAdminPassword = readEnvironmentVariable('MYSQL_ADMIN_PASSWORD', '')
param jwtSecretKey = readEnvironmentVariable('JWT_SECRET_KEY', '')
param smtpHost = readEnvironmentVariable('SMTP_HOST', '')
param smtpUser = readEnvironmentVariable('SMTP_USER', '')
param smtpPassword = readEnvironmentVariable('SMTP_PASSWORD', '')
param smtpFromAddress = readEnvironmentVariable('SMTP_FROM_ADDRESS', 'no-reply@localhost')

param mysqlSku = {
  name: 'Standard_D2ads_v5'
  tier: 'GeneralPurpose'
}
param mysqlStorageGb = 64
param mysqlBackupRetentionDays = 35
param mysqlGeoRedundantBackup = true
param storageSku = 'Standard_GZRS'
param keyVaultPurgeProtection = true
