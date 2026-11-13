using '../operations.bicep'

param environment = 'prod'
param location = readEnvironmentVariable('ASC_AZURE_LOCATION')
param owner = readEnvironmentVariable('ASC_RESOURCE_OWNER')
param workspaceName = 'ai-sous-chef-prod-logs'
param applicationInsightsName = 'ai-sous-chef-prod-insights'
param routing = { mode: 'disabled' }
