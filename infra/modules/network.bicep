// Virtual network: one subnet for the Container Apps environment, one delegated to MySQL, so the
// database has no public endpoint.

param prefix string
param location string
param addressPrefix string
param tags object

// /23 for the apps (the environment needs at least /27; room to grow), /24 for MySQL.
var appsPrefix = cidrSubnet(addressPrefix, 23, 0)
var mysqlPrefix = cidrSubnet(addressPrefix, 24, 2)

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-${prefix}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [addressPrefix]
    }
    subnets: [
      {
        name: 'snet-apps'
        properties: {
          addressPrefix: appsPrefix
          delegations: [
            {
              name: 'apps'
              properties: { serviceName: 'Microsoft.App/environments' }
            }
          ]
        }
      }
      {
        name: 'snet-mysql'
        properties: {
          addressPrefix: mysqlPrefix
          delegations: [
            {
              name: 'mysql'
              properties: { serviceName: 'Microsoft.DBforMySQL/flexibleServers' }
            }
          ]
        }
      }
    ]
  }
}

output vnetId string = vnet.id
output appsSubnetId string = vnet.properties.subnets[0].id
output mysqlSubnetId string = vnet.properties.subnets[1].id
