using '../main.bicep'

param environment = 'dev'
param location = 'eastus2'
param appServiceLocation = 'centralus'
param owner = 'AgamMakkar21'
param cosmosEndpoint = 'https://ai-sous-chef-dev-u46htzcnodzaw.documents.azure.com:443/'
param cosmosDatabase = 'sous-chef'
param authIssuer = readEnvironmentVariable('ASC_AUTH_ISSUER')
param authJwksUri = readEnvironmentVariable('ASC_AUTH_JWKS_URI')
param authClientId = readEnvironmentVariable('ASC_AUTH_CLIENT_ID')
param authOrigin = readEnvironmentVariable('ASC_AUTH_ORIGIN')
param authSecretUri = readEnvironmentVariable('ASC_AUTH_SECRET_URI')
