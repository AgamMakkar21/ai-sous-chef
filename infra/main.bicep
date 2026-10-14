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

@description('Region for the App Service plan and app; may differ from other resources where quota requires.')
@minLength(1)
param appServiceLocation string

@description('Linux App Service plan SKU for the single web/API app.')
@allowed([
  'B1'
  'B2'
  'B3'
  'P0v3'
  'P1v3'
])
param appServiceSku string = 'B1'

@description('Nonsecret Cosmos DB endpoint the app connects to.')
@minLength(1)
param cosmosEndpoint string

@description('Cosmos DB database name the app uses.')
@minLength(1)
param cosmosDatabase string

@description('Exact issuer from the approved external tenant OpenID metadata, not a workforce tenant.')
@minLength(1)
param authIssuer string

@description('jwks_uri from that same approved external tenant metadata.')
@minLength(1)
param authJwksUri string

@description('Consumer web app registration client ID, separate from workload/agent identities.')
@minLength(36)
@maxLength(36)
param authClientId string

@description('Approved browser HTTPS origin, using the actual App Service hostname or custom domain.')
@minLength(1)
param authOrigin string

@description('HTTPS Key Vault secret URI containing the external-tenant client credential; never its value.')
@minLength(1)
param authSecretUri string

var project = 'ai-sous-chef'
var namePrefix = '${project}-${environment}'
var foundryDeploymentNames = {
  orchestrator: 'orchestrator'
  recipeNutrition: 'recipe-nutrition'
  extraction: 'extraction'
}
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

module appService 'modules/app-service.bicep' = {
  name: '${namePrefix}-app-service'
  params: {
    location: appServiceLocation
    namePrefix: namePrefix
    tags: tags
    skuName: appServiceSku
    environment: environment
    cosmosEndpoint: cosmosEndpoint
    cosmosDatabase: cosmosDatabase
    authIssuer: authIssuer
    authJwksUri: authJwksUri
    authClientId: authClientId
    authOrigin: authOrigin
    authSecretUri: authSecretUri
    foundryProjectEndpoint: foundry.outputs.projectEndpoint
    foundryDeploymentNames: foundryDeploymentNames
    applicationInsightsResourceId: monitoring.outputs.applicationInsightsResourceId
  }
}

output appServiceResourceId string = appService.outputs.appServiceResourceId
output appServiceHostName string = appService.outputs.appServiceHostName
output appIdentityPrincipalId string = appService.outputs.appIdentityPrincipalId
output appIdentityClientId string = appService.outputs.appIdentityClientId
output foundryProjectEndpoint string = foundry.outputs.projectEndpoint
output foundryProjectPrincipalId string = foundry.outputs.projectPrincipalId
output resourceGroupId string = resourceGroup().id
output logAnalyticsResourceId string = monitoring.outputs.logAnalyticsResourceId
output applicationInsightsResourceId string = monitoring.outputs.applicationInsightsResourceId
