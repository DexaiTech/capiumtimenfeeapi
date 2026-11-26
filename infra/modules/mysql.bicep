// Azure Database for MySQL – Flexible Server, reachable only inside the virtual network.
// TLS is required; the database is utf8mb4 with a case- and accent-sensitive collation so that
// comparisons and unique keys behave as the application expects.

param name string
param location string
param version string
param sku object
param storageGb int
param backupRetentionDays int
param geoRedundantBackup bool
param adminLogin string

@secure()
param adminPassword string

param subnetId string
param vnetId string
param tags object

param databaseName string = 'capium'

resource dnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: '${name}.private.mysql.database.azure.com'
  location: 'global'
  tags: tags
}

resource dnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: dnsZone
  name: 'vnet'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnetId }
  }
}

resource server 'Microsoft.DBforMySQL/flexibleServers@2023-12-30' = {
  name: name
  location: location
  tags: tags
  sku: sku
  properties: {
    version: version
    administratorLogin: adminLogin
    administratorLoginPassword: adminPassword
    storage: {
      storageSizeGB: storageGb
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: backupRetentionDays
      geoRedundantBackup: geoRedundantBackup ? 'Enabled' : 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      delegatedSubnetResourceId: subnetId
      privateDnsZoneResourceId: dnsZone.id
    }
  }
  dependsOn: [dnsLink]
}

// The server accepts one configuration change at a time, so these run in sequence.
resource secureTransport 'Microsoft.DBforMySQL/flexibleServers/configurations@2023-12-30' = {
  parent: server
  name: 'require_secure_transport'
  properties: { value: 'ON', source: 'user-override' }
}

// PostgreSQL's default, which the application's locking was written for (no gap locks).
resource isolation 'Microsoft.DBforMySQL/flexibleServers/configurations@2023-12-30' = {
  parent: server
  name: 'transaction_isolation'
  properties: { value: 'READ-COMMITTED', source: 'user-override' }
  dependsOn: [secureTransport]
}

// The schema's triggers (overlapping periods) are created by the migration user; with binary
// logging on, MySQL refuses that unless function creators are trusted.
resource triggers 'Microsoft.DBforMySQL/flexibleServers/configurations@2023-12-30' = {
  parent: server
  name: 'log_bin_trust_function_creators'
  properties: { value: 'ON', source: 'user-override' }
  dependsOn: [isolation]
}

resource database 'Microsoft.DBforMySQL/flexibleServers/databases@2023-12-30' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'utf8mb4'
    collation: 'utf8mb4_0900_as_cs'
  }
  dependsOn: [triggers]
}

output fqdn string = server.properties.fullyQualifiedDomainName
output databaseName string = database.name
