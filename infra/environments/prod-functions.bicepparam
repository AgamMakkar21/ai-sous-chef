using '../functions.bicep'

param environment = 'prod'
param location = readEnvironmentVariable('ASC_AZURE_LOCATION')
param owner = readEnvironmentVariable('ASC_RESOURCE_OWNER')
param cosmosAccountName = readEnvironmentVariable('ASC_COSMOS_ACCOUNT_NAME')
param applicationInsightsName = readEnvironmentVariable('ASC_APPLICATION_INSIGHTS_NAME')
param logAnalyticsName = readEnvironmentVariable('ASC_LOG_ANALYTICS_NAME')
param integrationSubnetResourceId = readEnvironmentVariable('ASC_FUNCTION_INTEGRATION_SUBNET_RESOURCE_ID')
