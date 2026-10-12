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

output resourceGroupId string = resourceGroup().id
output logAnalyticsResourceId string = monitoring.outputs.logAnalyticsResourceId
output applicationInsightsResourceId string = monitoring.outputs.applicationInsightsResourceId
