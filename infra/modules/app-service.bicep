@description('Approved hosting region.')
param location string

@description('Project and environment-specific resource prefix.')
param namePrefix string

param tags object

@description('Linux App Service plan SKU. One instance; scaling is a deliberate later change.')
@allowed([
  'B1'
  'B2'
  'B3'
  'P0v3'
  'P1v3'
])
param skuName string

@description('Environment name passed to the app as ASC_ENVIRONMENT.')
@allowed([
  'dev'
  'prod'
])
param environment string

@description('Nonsecret Cosmos DB endpoint (HTTPS).')
param cosmosEndpoint string

@description('Cosmos DB database name.')
param cosmosDatabase string

@description('Nonsecret New Foundry project endpoint (HTTPS).')
param foundryProjectEndpoint string

@description('Model deployment names the app calls, keyed by role.')
param foundryDeploymentNames object

@description('Application Insights resource ID; its connection string is nonsecret.')
param applicationInsightsResourceId string

var appName = '${namePrefix}-web-${take(uniqueString(resourceGroup().id), 6)}'
var pythonVersion = '3.12'
// The app is deployed as source; --app-dir makes backend/src importable without installing it.
var startupCommand = 'python -m uvicorn sous_chef.app:create_app --factory --app-dir backend/src --host 0.0.0.0 --port 8000 --no-access-log'

resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: last(split(applicationInsightsResourceId, '/'))
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-app-identity'
  location: location
  tags: tags
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: '${namePrefix}-plan'
  location: location
  tags: tags
  kind: 'linux'
  sku: {
    name: skuName
    capacity: 1
  }
  properties: {
    reserved: true
  }
}

resource app 'Microsoft.Web/sites@2024-04-01' = {
  name: appName
  location: location
  tags: tags
  kind: 'app,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    clientAffinityEnabled: false
    publicNetworkAccess: 'Enabled'
    siteConfig: {
      linuxFxVersion: 'PYTHON|${pythonVersion}'
      appCommandLine: startupCommand
      alwaysOn: true
      http20Enabled: true
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      healthCheckPath: '/health'
      appSettings: [
        { name: 'ASC_ENVIRONMENT', value: environment }
        { name: 'ASC_ADAPTER_MODE', value: 'live' }
        { name: 'ASC_COSMOS_ENDPOINT', value: cosmosEndpoint }
        { name: 'ASC_COSMOS_DATABASE', value: cosmosDatabase }
        { name: 'ASC_FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }
        { name: 'ASC_FOUNDRY_ORCHESTRATOR_DEPLOYMENT', value: foundryDeploymentNames.orchestrator }
        {
          name: 'ASC_FOUNDRY_RECIPE_NUTRITION_DEPLOYMENT'
          value: foundryDeploymentNames.recipeNutrition
        }
        { name: 'ASC_FOUNDRY_EXTRACTION_DEPLOYMENT', value: foundryDeploymentNames.extraction }
        { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
        { name: 'ApplicationInsightsAgent_EXTENSION_VERSION', value: 'disabled' }
        { name: 'AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED', value: 'false' }
        { name: 'OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT', value: 'false' }
        { name: 'OTEL_SDK_DISABLED', value: 'true' }
        { name: 'OTEL_TRACES_EXPORTER', value: 'none' }
        { name: 'OTEL_METRICS_EXPORTER', value: 'none' }
        { name: 'OTEL_LOGS_EXPORTER', value: 'none' }
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
      ]
    }
  }
}

// Deployments use Entra tokens (az webapp deploy), not shared publishing passwords.
resource ftpPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: app
  name: 'ftp'
  properties: {
    allow: false
  }
}

resource scmPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: app
  name: 'scm'
  properties: {
    allow: false
  }
}

output appServiceResourceId string = app.id
output appServiceHostName string = app.properties.defaultHostName
output appIdentityPrincipalId string = identity.properties.principalId
output appIdentityClientId string = identity.properties.clientId
output appIdentityResourceId string = identity.id
