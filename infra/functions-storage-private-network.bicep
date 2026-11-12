targetScope = 'resourceGroup'

@description('Region of the existing VNet/private-endpoint subnet.')
@minLength(1)
param location string

@description('Existing dev Functions host storage account. Public access and RBAC are unchanged.')
@minLength(3)
param storageAccountName string

@description('Existing VNet used by the feedback Function integration.')
@minLength(1)
param virtualNetworkResourceId string

@description('Existing nondelegated private-endpoint subnet in that VNet.')
@minLength(1)
param privateEndpointSubnetResourceId string

var prefix = 'ai-sous-chef-dev-feedback-storage'
var tags = {
  project: 'ai-sous-chef'
  environment: 'dev'
  managedBy: 'bicep'
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.blob.${environment().suffixes.storage}'
  location: 'global'
  tags: tags
}

resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: zone
  name: prefix
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetworkResourceId
    }
  }
}

resource endpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${prefix}-pe'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointSubnetResourceId
    }
    privateLinkServiceConnections: [
      {
        name: prefix
        properties: {
          privateLinkServiceId: storage.id
          groupIds: ['blob']
        }
      }
    ]
  }
}

resource dns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'host-blob'
        properties: {
          privateDnsZoneId: zone.id
        }
      }
    ]
  }
}

output privateEndpointResourceId string = endpoint.id
output hostStorageHostName string = '${storage.name}.blob.${environment().suffixes.storage}'
