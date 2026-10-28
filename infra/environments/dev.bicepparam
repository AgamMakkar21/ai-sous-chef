using '../main.bicep'

param environment = 'dev'
param location = 'eastus2'
param appServiceLocation = 'centralus'
param appServiceIntegrationSubnetResourceId = readEnvironmentVariable('ASC_APP_SERVICE_INTEGRATION_SUBNET_RESOURCE_ID', '')
param owner = 'AgamMakkar21'
param cosmosEndpoint = 'https://ai-sous-chef-dev-u46htzcnodzaw.documents.azure.com:443/'
param cosmosDatabase = 'sous-chef'
param authMode = readEnvironmentVariable('ASC_AUTH_MODE', 'external_id')
param authIssuer = readEnvironmentVariable('ASC_AUTH_ISSUER', '')
param authJwksUri = readEnvironmentVariable('ASC_AUTH_JWKS_URI', '')
param socialAuth = json(readEnvironmentVariable('ASC_SOCIAL_AUTH_CONFIG', '{}'))
param authClientId = readEnvironmentVariable('ASC_AUTH_CLIENT_ID')
param authOrigin = readEnvironmentVariable('ASC_AUTH_ORIGIN')
