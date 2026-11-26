// Log Analytics: container stdout (structlog JSON) from the API, worker, UI and jobs.

param prefix string
param location string
param tags object

@description('Days logs are kept.')
param retentionDays int = 30

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${prefix}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: retentionDays
  }
}

output workspaceName string = workspace.name
