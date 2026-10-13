targetScope = 'resourceGroup'

@description('Cloud environment. Local development does not provision Azure resources.')
@allowed([
  'dev'
  'prod'
])
param environment string

@description('Owner-approved Azure region; no implicit subscription or region selection.')
@minLength(1)
param location string

@description('Accountable resource owner or team, without personal contact details.')
@minLength(1)
param owner string

@description('Model deployments for the MVP agents. Names are app configuration; models are separate.')
param modelDeployments array = [
  {
    name: 'orchestrator'
    model: 'gpt-5.4-mini'
    version: '2026-03-17'
    sku: 'GlobalStandard'
  }
  {
    name: 'recipe-nutrition'
    model: 'gpt-5.4'
    version: '2026-03-05'
    sku: 'GlobalStandard'
  }
  {
    name: 'extraction'
    model: 'gpt-5.4-nano'
    version: '2026-03-17'
    sku: 'GlobalStandard'
  }
]

var project = 'ai-sous-chef'
var namePrefix = '${project}-${environment}'
var tags = {
  project: project
  environment: environment
  owner: owner
  managedBy: 'bicep'
}

module monitoring 'modules/monitoring.bicep' = {
  name: '${namePrefix}-monitoring'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
  }
}

module foundry 'modules/foundry.bicep' = {
  name: '${namePrefix}-foundry'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
    deployments: modelDeployments
  }
}

output foundryProjectEndpoint string = foundry.outputs.projectEndpoint
output foundryProjectPrincipalId string = foundry.outputs.projectPrincipalId
output resourceGroupId string = resourceGroup().id
output logAnalyticsResourceId string = monitoring.outputs.logAnalyticsResourceId
output applicationInsightsResourceId string = monitoring.outputs.applicationInsightsResourceId
