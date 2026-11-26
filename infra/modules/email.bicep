// Optional: Azure Communication Services Email with an Azure-managed sender domain
// (DoNotReply@<guid>.azurecomm.net). The API keeps sending through SMTP: after deploying, create
// an SMTP username for the Communication Services resource (an Entra application) and set
// SMTP_HOST=smtp.azurecomm.net, SMTP_PORT=587, SMTP_USER and SMTP_PASSWORD. See infra/README.md.

param prefix string
param dataLocation string
param tags object

resource emailService 'Microsoft.Communication/emailServices@2023-04-01' = {
  name: 'ecs-${prefix}'
  location: 'global'
  tags: tags
  properties: { dataLocation: dataLocation }
}

resource domain 'Microsoft.Communication/emailServices/domains@2023-04-01' = {
  parent: emailService
  name: 'AzureManagedDomain'
  location: 'global'
  tags: tags
  properties: {
    domainManagement: 'AzureManaged'
    userEngagementTracking: 'Disabled'
  }
}

resource communication 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: 'acs-${prefix}'
  location: 'global'
  tags: tags
  properties: {
    dataLocation: dataLocation
    linkedDomains: [domain.id]
  }
}

output senderDomain string = domain.properties.mailFromSenderDomain
output communicationServiceName string = communication.name
