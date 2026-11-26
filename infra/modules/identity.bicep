// One user-assigned managed identity for the API, worker, UI and jobs: pulls images, reads the
// vault's secrets and reads and writes uploaded files. No keys or passwords leave Azure.

param prefix string
param location string
param tags object

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}'
  location: location
  tags: tags
}

output id string = identity.id
output principalId string = identity.properties.principalId
output clientId string = identity.properties.clientId
