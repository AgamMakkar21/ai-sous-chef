targetScope = 'resourceGroup'

@allowed(['dev'])
param environment string

@minLength(1)
param location string

@minLength(1)
param owner string

@description('Existing Cosmos account in this resource group; never provision a second database.')
param cosmosAccountName string

@description('Existing monitoring resources from the foundation outputs.')
param applicationInsightsName string
param logAnalyticsName string

@description('Existing owner-approved delegated subnet; required for a private-only Cosmos account.')
param integrationSubnetResourceId string = ''

var namePrefix = 'ai-sous-chef-${environment}'
var tags = {
  project: 'ai-sous-chef'
  environment: environment
  owner: owner
  managedBy: 'bicep'
}

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2025-04-15' existing = {
  name: cosmosAccountName
}

resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsName
}

module worker 'modules/functions.bicep' = {
  name: '${namePrefix}-feedback-host'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
    cosmosEndpoint: cosmos.properties.documentEndpoint
    applicationInsightsName: insights.name
    logAnalyticsResourceId: workspace.id
    integrationSubnetResourceId: integrationSubnetResourceId
  }
}

module access 'modules/functions-cosmos-access.bicep' = {
  name: '${namePrefix}-feedback-access'
  params: {
    accountName: cosmos.name
    principalId: worker.outputs.principalId
  }
}

output functionAppName string = worker.outputs.appName
output functionAppResourceId string = worker.outputs.appResourceId
output functionIdentityPrincipalId string = worker.outputs.principalId
output functionIdentityClientId string = worker.outputs.clientId
output hostStorageResourceId string = worker.outputs.storageResourceId
