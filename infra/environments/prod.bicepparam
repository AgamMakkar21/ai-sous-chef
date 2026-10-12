using '../main.bicep'

param environment = 'prod'
param location = readEnvironmentVariable('ASC_AZURE_LOCATION')
param owner = readEnvironmentVariable('ASC_RESOURCE_OWNER')
