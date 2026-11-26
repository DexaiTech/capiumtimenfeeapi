// Development environment. Secrets come from the shell, never from this file:
//   export MYSQL_ADMIN_PASSWORD=... JWT_SECRET_KEY=...
//   az deployment group create -g rg-capium-dev -f infra/main.bicep -p infra/env/dev.bicepparam
using '../main.bicep'

param environmentName = 'dev'
param deployApps = bool(readEnvironmentVariable('DEPLOY_APPS', 'true'))
param apiImageTag = readEnvironmentVariable('API_IMAGE_TAG', 'latest')
param uiImageTag = readEnvironmentVariable('UI_IMAGE_TAG', 'latest')

param mysqlAdminPassword = readEnvironmentVariable('MYSQL_ADMIN_PASSWORD', '')
param jwtSecretKey = readEnvironmentVariable('JWT_SECRET_KEY', '')
param smtpPassword = readEnvironmentVariable('SMTP_PASSWORD', '')

param mysqlSku = {
  name: 'Standard_B1ms'
  tier: 'Burstable'
}
param mysqlBackupRetentionDays = 7
param storageSku = 'Standard_LRS'
param keyVaultPurgeProtection = false
