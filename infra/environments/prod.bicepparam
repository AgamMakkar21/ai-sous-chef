using '../main.bicep'

param environment = 'prod'
param location = readEnvironmentVariable('ASC_AZURE_LOCATION')
param appServiceLocation = readEnvironmentVariable('ASC_APP_SERVICE_LOCATION')
param owner = readEnvironmentVariable('ASC_RESOURCE_OWNER')
param cosmosEndpoint = readEnvironmentVariable('ASC_COSMOS_ENDPOINT')
param cosmosDatabase = readEnvironmentVariable('ASC_COSMOS_DATABASE')
param authMode = readEnvironmentVariable('ASC_AUTH_MODE', 'external_id')
param authIssuer = readEnvironmentVariable('ASC_AUTH_ISSUER', '')
param authJwksUri = readEnvironmentVariable('ASC_AUTH_JWKS_URI', '')
param socialAuth = json(readEnvironmentVariable('ASC_SOCIAL_AUTH_CONFIG', '{}'))
param authClientId = readEnvironmentVariable('ASC_AUTH_CLIENT_ID')
param authOrigin = readEnvironmentVariable('ASC_AUTH_ORIGIN')
