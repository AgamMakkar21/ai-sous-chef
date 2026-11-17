targetScope = 'resourceGroup'

@allowed(['dev', 'prod'])
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

@description('Existing owner-approved Foundry account and project; no model is provisioned here.')
@minLength(1)
param foundryAccountName string
@minLength(1)
param foundryProjectName string

@description('Explicit existing extraction deployment and expected GPT-5.4 nano backing model ID.')
@minLength(1)
param extractionDeploymentName string
@minLength(1)
param extractionModelId string

@description('Existing owner-approved delegated subnet; required for a private-only Cosmos account.')
param integrationSubnetResourceId string = ''

@description('Reviewed opt-in to app-owned asynchronous learning telemetry.')
param telemetryEnabled bool = false

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

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: foundry
  name: foundryProjectName
}

module worker 'modules/functions.bicep' = {
  name: '${namePrefix}-feedback-host'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
    environment: environment
    cosmosEndpoint: cosmos.properties.documentEndpoint
    applicationInsightsName: insights.name
    logAnalyticsResourceId: workspace.id
    integrationSubnetResourceId: integrationSubnetResourceId
    telemetryEnabled: telemetryEnabled
    foundryProjectEndpoint: 'https://${foundryAccountName}.services.ai.azure.com/api/projects/${foundryProjectName}'
    extractionDeploymentName: extractionDeploymentName
    extractionModelId: extractionModelId
  }
}

resource modelInvocationGrant 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, '${namePrefix}-feedback-identity', 'foundry-user')
  scope: project
  properties: {
    principalId: worker.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
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
