// Key Vault for the application secrets. The container apps reference them by URI through the
// managed identity, so a rotated secret is picked up by the next revision without a redeploy.

param name string
param location string
param purgeProtection bool
param readerPrincipalId string
param tags object

@secure()
param dbUrl string

@secure()
param jwtSecretKey string

@secure()
param smtpPassword string

var secretsUserRole = '4633458b-17de-408a-b874-0445c86b69e6'

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: purgeProtection ? true : null
  }
}

resource dbUrlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'db-url'
  properties: { value: dbUrl }
}

resource jwtSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'jwt-secret-key'
  properties: { value: jwtSecretKey }
}

// Only with an SMTP relay; without one the apps get no SMTP_PASSWORD at all.
resource smtpSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(smtpPassword)) {
  parent: vault
  name: 'smtp-password'
  properties: { value: smtpPassword }
}

resource readSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, readerPrincipalId, secretsUserRole)
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsUserRole)
    principalId: readerPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output name string = vault.name
output dbUrlSecretUri string = dbUrlSecret.properties.secretUri
output jwtSecretUri string = jwtSecret.properties.secretUri
output smtpPasswordSecretUri string = empty(smtpPassword) ? '' : smtpSecret!.properties.secretUri
