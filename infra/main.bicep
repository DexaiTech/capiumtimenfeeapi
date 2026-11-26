// Capium Time & Fees on Azure: one resource group per environment.
//
//   az deployment group create -g <rg> -f infra/main.bicep -p infra/env/dev.bicepparam
//
// First deployment of a new environment: deployApps=false (registry, vault, database, storage and
// the Container Apps environment), push the images, then deploy again with deployApps=true.
// See infra/README.md.

targetScope = 'resourceGroup'

@description('Short environment name, used in resource names (dev, test, prod).')
@minLength(2)
@maxLength(8)
param environmentName string

@description('Azure region for every resource except the global email service.')
param location string = resourceGroup().location

@description('Create the container apps and the migration job. False on the very first deployment, before the images exist in the registry.')
param deployApps bool = true

@description('API image tag in the registry (capium-api:<tag>), normally the commit SHA.')
param apiImageTag string = 'latest'

@description('UI image tag in the registry (capium-ui:<tag>).')
param uiImageTag string = 'latest'

@description('Public origin of the UI, for example https://timenfee.example.com. Empty: the UI container app\'s own address.')
param frontendUrl string = ''

@description('One practice and no public sign-up (CLOSED_DEPLOYMENT).')
param closedDeployment bool = true

@description('MySQL administrator login; the API connects with it (see infra/README.md for a dedicated user).')
param mysqlAdminLogin string = 'capiumadmin'

@description('MySQL administrator password (12+ characters, kept in Key Vault).')
@secure()
@minLength(12)
param mysqlAdminPassword string

@description('MySQL server version.')
param mysqlVersion string = '8.4'

@description('MySQL compute: name and tier, for example Standard_B1ms / Burstable or Standard_D2ads_v5 / GeneralPurpose.')
param mysqlSku object = {
  name: 'Standard_B1ms'
  tier: 'Burstable'
}

@description('MySQL storage in GB (grows automatically).')
param mysqlStorageGb int = 32

@description('Days of point-in-time restore for the database.')
@minValue(1)
@maxValue(35)
param mysqlBackupRetentionDays int = 7

@description('Copy database backups to the paired region.')
param mysqlGeoRedundantBackup bool = false

@description('JWT signing key (32+ characters, kept in Key Vault).')
@secure()
@minLength(32)
param jwtSecretKey string

@description('Fernet key that encrypts two-factor secrets (44 characters, kept in Key Vault). Changing it makes stored secrets unreadable.')
@secure()
@minLength(44)
@maxLength(44)
param mfaEncryptionKey string

@description('SMTP relay host. Empty: the mock gateway records messages instead of sending them.')
param smtpHost string = ''

param smtpPort int = 587
param smtpUser string = ''

@secure()
param smtpPassword string = ''

param smtpFromAddress string = 'no-reply@localhost'
param smtpFromName string = 'Capium Time & Fees'

@description('Storage redundancy for uploaded files.')
@allowed([
  'Standard_LRS'
  'Standard_ZRS'
  'Standard_GRS'
  'Standard_GZRS'
])
param storageSku string = 'Standard_ZRS'

@description('Key Vault purge protection (cannot be turned off again; use it in production).')
param keyVaultPurgeProtection bool = false

@description('Also create Azure Communication Services Email with an Azure-managed sender domain.')
param deployEmail bool = false

@description('Where Azure Communication Services keeps its data.')
param emailDataLocation string = 'UK'

@description('Address space of the virtual network; the two subnets are carved from it.')
param vnetAddressPrefix string = '10.40.0.0/16'

param tags object = {}

var prefix = 'capium-${environmentName}'
var suffix = substring(uniqueString(resourceGroup().id), 0, 6)
var allTags = union(tags, { application: 'capium-timenfee', environment: environmentName })

module network 'modules/network.bicep' = {
  name: 'network'
  params: {
    prefix: prefix
    location: location
    addressPrefix: vnetAddressPrefix
    tags: allTags
  }
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    prefix: prefix
    location: location
    tags: allTags
  }
}

module identity 'modules/identity.bicep' = {
  name: 'identity'
  params: {
    prefix: prefix
    location: location
    tags: allTags
  }
}

module registry 'modules/registry.bicep' = {
  name: 'registry'
  params: {
    name: 'crcapium${environmentName}${suffix}'
    location: location
    pullPrincipalId: identity.outputs.principalId
    tags: allTags
  }
}

module mysql 'modules/mysql.bicep' = {
  name: 'mysql'
  params: {
    name: 'mysql-${prefix}-${suffix}'
    location: location
    version: mysqlVersion
    sku: mysqlSku
    storageGb: mysqlStorageGb
    backupRetentionDays: mysqlBackupRetentionDays
    geoRedundantBackup: mysqlGeoRedundantBackup
    adminLogin: mysqlAdminLogin
    adminPassword: mysqlAdminPassword
    subnetId: network.outputs.mysqlSubnetId
    vnetId: network.outputs.vnetId
    tags: allTags
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    name: 'stcapium${environmentName}${suffix}'
    location: location
    sku: storageSku
    dataPrincipalId: identity.outputs.principalId
    tags: allTags
  }
}

module vault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: {
    name: 'kv-${prefix}-${suffix}'
    location: location
    purgeProtection: keyVaultPurgeProtection
    readerPrincipalId: identity.outputs.principalId
    // The API normalises mysql:// to its async driver and adds TLS from DB_SSL_MODE.
    dbUrl: 'mysql://${mysqlAdminLogin}:${uriComponent(mysqlAdminPassword)}@${mysql.outputs.fqdn}:3306/${mysql.outputs.databaseName}'
    jwtSecretKey: jwtSecretKey
    mfaEncryptionKey: mfaEncryptionKey
    smtpPassword: smtpPassword
    tags: allTags
  }
}

module email 'modules/email.bicep' = if (deployEmail) {
  name: 'email'
  params: {
    prefix: prefix
    dataLocation: emailDataLocation
    tags: allTags
  }
}

module apps 'modules/apps.bicep' = if (deployApps) {
  name: 'apps'
  params: {
    prefix: prefix
    location: location
    infrastructureSubnetId: network.outputs.appsSubnetId
    logAnalyticsWorkspaceName: monitoring.outputs.workspaceName
    identityId: identity.outputs.id
    identityClientId: identity.outputs.clientId
    registryServer: registry.outputs.loginServer
    apiImage: '${registry.outputs.loginServer}/capium-api:${apiImageTag}'
    workerImage: '${registry.outputs.loginServer}/capium-worker:${apiImageTag}'
    uiImage: '${registry.outputs.loginServer}/capium-ui:${uiImageTag}'
    frontendUrl: frontendUrl
    closedDeployment: closedDeployment
    dbUrlSecretUri: vault.outputs.dbUrlSecretUri
    jwtSecretUri: vault.outputs.jwtSecretUri
    mfaKeySecretUri: vault.outputs.mfaKeySecretUri
    smtpPasswordSecretUri: vault.outputs.smtpPasswordSecretUri
    smtpHost: smtpHost
    smtpPort: smtpPort
    smtpUser: smtpUser
    smtpFromAddress: smtpFromAddress
    smtpFromName: smtpFromName
    blobEndpoint: storage.outputs.blobEndpoint
    blobContainer: storage.outputs.containerName
    tags: allTags
  }
}

output registryName string = registry.outputs.name
output registryLoginServer string = registry.outputs.loginServer
output keyVaultName string = vault.outputs.name
output mysqlHost string = mysql.outputs.fqdn
output storageAccountName string = storage.outputs.name
output uiUrl string = deployApps ? apps!.outputs.uiUrl : ''
output apiAppName string = '${prefix}-api'
output workerAppName string = '${prefix}-worker'
output migrateJobName string = '${prefix}-migrate'
