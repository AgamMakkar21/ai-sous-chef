targetScope = 'resourceGroup'

@description('Region of the existing private-endpoint subnet/VNet, not the Cosmos account.')
@minLength(1)
param location string

@minLength(1)
param owner string

@description('Existing dev Cosmos account in this resource group. Its public access is never changed.')
param cosmosAccountName string

@description('Existing dev apps in this resource group.')
param functionAppName string
param webAppName string

@description('Separate existing subnets for the two App Service plans, each delegated to Microsoft.Web/serverFarms.')
@minLength(1)
param workerIntegrationSubnetResourceId string
@minLength(1)
param webIntegrationSubnetResourceId string

@description('Existing nondelegated subnet approved for private endpoints, separate from integration subnets.')
@minLength(1)
param privateEndpointSubnetResourceId string

@description('Existing privatelink.documents.azure.com zone ID. The network owner must link it to client VNets or configure DNS forwarding.')
@minLength(1)
param privateDnsZoneResourceId string

var namePrefix = 'ai-sous-chef-dev'

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2025-04-15' existing = {
  name: cosmosAccountName
}

resource endpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${namePrefix}-cosmos-pe'
  location: location
  tags: {
    project: 'ai-sous-chef'
    environment: 'dev'
    owner: owner
    managedBy: 'bicep'
  }
  properties: {
    subnet: {
      id: privateEndpointSubnetResourceId
    }
    privateLinkServiceConnections: [
      {
        name: '${namePrefix}-cosmos'
        properties: {
          privateLinkServiceId: cosmos.id
          groupIds: ['Sql']
        }
      }
    ]
  }
}

// The zone group manages all account/regional records; do not hardcode a single A record.
resource dns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'cosmos-nosql'
        properties: {
          privateDnsZoneId: privateDnsZoneResourceId
        }
      }
    ]
  }
}

module workerIntegration 'modules/site-vnet.bicep' = {
  name: '${namePrefix}-worker-vnet'
  params: {
    appName: functionAppName
    integrationSubnetResourceId: workerIntegrationSubnetResourceId
  }
}

module webIntegration 'modules/site-vnet.bicep' = {
  name: '${namePrefix}-web-vnet'
  params: {
    appName: webAppName
    integrationSubnetResourceId: webIntegrationSubnetResourceId
  }
}

output privateEndpointResourceId string = endpoint.id
output cosmosHostName string = '${cosmos.name}.documents.azure.com'
