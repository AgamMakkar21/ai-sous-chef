using '../main.bicep'

param environment = 'prod'
param location = readEnvironmentVariable('ASC_AZURE_LOCATION')
param appServiceLocation = readEnvironmentVariable('ASC_APP_SERVICE_LOCATION')
param owner = readEnvironmentVariable('ASC_RESOURCE_OWNER')
param cosmosEndpoint = readEnvironmentVariable('ASC_COSMOS_ENDPOINT')
param cosmosDatabase = readEnvironmentVariable('ASC_COSMOS_DATABASE')
