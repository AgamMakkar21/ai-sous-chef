targetScope = 'resourceGroup'

@allowed([
  'dev'
])
param environment string

@description('Owner-approved Cosmos region, independent of monitoring location.')
@minLength(1)
param location string

@minLength(1)
param owner string

@description('Approved IPv4 addresses/CIDRs for backend egress and temporary operators. Empty disables public access.')
param approvedIpRanges array = []

@description('Explicit demo-only opt-in to public access from all IPs. Entra auth remains mandatory.')
param allowAllPublicNetwork bool = false

@description('Approved Entra object IDs granted product-container data access; never browser or agent identities.')
param productWriterPrincipalIds array = []

var namePrefix = 'ai-sous-chef-${environment}'

module cosmos 'modules/cosmos.bicep' = {
  name: '${namePrefix}-cosmos-resources'
  params: {
    accountName: '${namePrefix}-${uniqueString(subscription().subscriptionId, resourceGroup().id)}'
    location: location
    approvedIpRanges: approvedIpRanges
    allowAllPublicNetwork: allowAllPublicNetwork
    productWriterPrincipalIds: productWriterPrincipalIds
    tags: {
      project: 'ai-sous-chef'
      environment: environment
      owner: owner
      managedBy: 'bicep'
    }
  }
}

output cosmosAccountResourceId string = cosmos.outputs.accountResourceId
output cosmosEndpoint string = cosmos.outputs.endpoint
output cosmosDatabaseName string = cosmos.outputs.databaseName
