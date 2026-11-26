// Container Apps: the API (internal ingress), the worker (no ingress), the UI (public HTTPS
// ingress, nginx forwarding /api to the API) and the manual migration job.

param prefix string
param location string
param infrastructureSubnetId string
param logAnalyticsWorkspaceName string
param identityId string
param identityClientId string
param registryServer string
param apiImage string
param workerImage string
param uiImage string
param frontendUrl string
param closedDeployment bool
param dbUrlSecretUri string
param jwtSecretUri string
param mfaKeySecretUri string
param smtpPasswordSecretUri string
param smtpHost string
param smtpPort int
param smtpUser string
param smtpFromAddress string
param smtpFromName string
param blobEndpoint string
param blobContainer string
param tags object

@description('Replicas of the API: at least one, so requests never wait for a cold start.')
param apiMinReplicas int = 1
param apiMaxReplicas int = 3

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${prefix}'
  location: location
  tags: tags
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: infrastructureSubnetId
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
  }
}

var uiName = '${prefix}-ui'
var publicUrl = empty(frontendUrl) ? 'https://${uiName}.${environment.properties.defaultDomain}' : frontendUrl
var identities = {
  type: 'UserAssigned'
  userAssignedIdentities: { '${identityId}': {} }
}
var registries = [
  {
    server: registryServer
    identity: identityId
  }
]
var hasSmtpPassword = !empty(smtpPasswordSecretUri)

// Secrets live in Key Vault; the apps hold references only.
var secrets = concat(
  [
    { name: 'db-url', keyVaultUrl: dbUrlSecretUri, identity: identityId }
    { name: 'jwt-secret-key', keyVaultUrl: jwtSecretUri, identity: identityId }
    { name: 'mfa-encryption-key', keyVaultUrl: mfaKeySecretUri, identity: identityId }
  ],
  hasSmtpPassword ? [{ name: 'smtp-password', keyVaultUrl: smtpPasswordSecretUri, identity: identityId }] : []
)

// The API, the worker and the migration job share one configuration (docs/deployment.md).
var backendEnv = concat(
  [
    { name: 'APP_ENV', value: 'production' }
    { name: 'APP_DEBUG', value: 'false' }
    { name: 'DB_URL', secretRef: 'db-url' }
    { name: 'DB_SSL_MODE', value: 'verify_identity' }
    { name: 'JWT_SECRET_KEY', secretRef: 'jwt-secret-key' }
    { name: 'MFA_ENCRYPTION_KEY', secretRef: 'mfa-encryption-key' }
    { name: 'FRONTEND_URL', value: publicUrl }
    // Only the SMTP greeting uses it; the API is reached through the UI's origin.
    { name: 'BACKEND_URL', value: publicUrl }
    { name: 'CORS_ORIGINS', value: publicUrl }
    { name: 'CLOSED_DEPLOYMENT', value: string(closedDeployment) }
    // Only the environment's own proxies can reach the API (internal ingress).
    { name: 'FORWARDED_ALLOW_IPS', value: '*' }
    { name: 'AZURE_CLIENT_ID', value: identityClientId }
    { name: 'AZURE_STORAGE_ACCOUNT_URL', value: blobEndpoint }
    { name: 'AZURE_STORAGE_CONTAINER', value: blobContainer }
    { name: 'SMTP_HOST', value: smtpHost }
    { name: 'SMTP_PORT', value: string(smtpPort) }
    { name: 'SMTP_USER', value: smtpUser }
    { name: 'SMTP_FROM_ADDRESS', value: smtpFromAddress }
    { name: 'SMTP_FROM_NAME', value: smtpFromName }
  ],
  hasSmtpPassword ? [{ name: 'SMTP_PASSWORD', secretRef: 'smtp-password' }] : []
)

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${prefix}-api'
  location: location
  tags: tags
  identity: identities
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        targetPort: 8000
        transport: 'http'
        allowInsecure: false
      }
      registries: registries
      secrets: secrets
    }
    template: {
      containers: [
        {
          name: 'api'
          image: apiImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: backendEnv
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/health/live', port: 8000 }
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: { path: '/health/ready', port: 8000 }
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: apiMinReplicas
        maxReplicas: apiMaxReplicas
        rules: [
          {
            name: 'http'
            http: { metadata: { concurrentRequests: '50' } }
          }
        ]
      }
    }
  }
}

resource worker 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${prefix}-worker'
  location: location
  tags: tags
  identity: identities
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      registries: registries
      secrets: secrets
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: workerImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: backendEnv
        }
      ]
      // Always running: it polls the job queue and runs the schedule (one scheduler at a time).
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

resource ui 'Microsoft.App/containerApps@2024-03-01' = {
  name: uiName
  location: location
  tags: tags
  identity: identities
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 80
        transport: 'http'
        allowInsecure: false
      }
      registries: registries
    }
    template: {
      containers: [
        {
          name: 'ui'
          image: uiImage
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            // nginx forwards /api here (docker/nginx.conf.template in the UI repository).
            { name: 'API_UPSTREAM', value: 'https://${api.properties.configuration.ingress.fqdn}' }
            { name: 'API_HOST', value: api.properties.configuration.ingress.fqdn }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 80 }
              periodSeconds: 30
            }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 3 }
    }
  }
}

// Run before every release: az containerapp job start -n <prefix>-migrate -g <rg>.
// The first practice is created with the same job and another command (infra/README.md).
resource migrate 'Microsoft.App/jobs@2024-03-01' = {
  name: '${prefix}-migrate'
  location: location
  tags: tags
  identity: identities
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 1800
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: registries
      secrets: secrets
    }
    template: {
      containers: [
        {
          name: 'migrate'
          image: apiImage
          command: ['alembic']
          args: ['upgrade', 'head']
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: backendEnv
        }
      ]
    }
  }
}

output uiUrl string = publicUrl
output apiInternalFqdn string = api.properties.configuration.ingress.fqdn
