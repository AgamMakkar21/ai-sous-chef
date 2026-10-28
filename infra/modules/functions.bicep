param location string
param namePrefix string
param tags object
param cosmosEndpoint string
param applicationInsightsName string
param logAnalyticsResourceId string
param integrationSubnetResourceId string = ''

var appName = '${namePrefix}-feedback-${take(uniqueString(resourceGroup().id), 6)}'

resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-feedback-identity'
  location: location
  tags: tags
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'ascfh${uniqueString(resourceGroup().id, namePrefix)}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'None'
    }
  }
}

// Host locks and host-key storage use blobs. No queues, tables or blob triggers in this slice.
resource hostStorageGrant 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identity.id, 'host-blob-owner')
  scope: storage
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')
  }
}

resource monitoringGrant 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(insights.id, identity.id, 'feedback-telemetry')
  scope: insights
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')
  }
}

// Dedicated Linux hosting avoids the Azure Files connection-string requirement.
resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: '${namePrefix}-feedback-plan'
  location: location
  tags: tags
  kind: 'linux'
  sku: { name: 'B1', capacity: 1 }
  properties: { reserved: true }
}

resource app 'Microsoft.Web/sites@2024-04-01' = {
  name: appName
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    clientAffinityEnabled: false
    siteConfig: {
      linuxFxVersion: 'Python|3.12'
      alwaysOn: true
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      ftpsState: 'Disabled'
      healthCheckPath: '/api/health'
      appSettings: [
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
        { name: 'AzureWebJobsStorage__accountName', value: storage.name }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'AzureWebJobsStorage__clientId', value: identity.properties.clientId }
        { name: 'FeedbackCosmos__accountEndpoint', value: cosmosEndpoint }
        { name: 'FeedbackCosmos__credential', value: 'managedidentity' }
        { name: 'FeedbackCosmos__clientId', value: identity.properties.clientId }
        { name: 'FeedbackLeases__accountEndpoint', value: cosmosEndpoint }
        { name: 'FeedbackLeases__credential', value: 'managedidentity' }
        { name: 'FeedbackLeases__clientId', value: identity.properties.clientId }
        { name: 'ASC_COSMOS_DATABASE', value: 'sous-chef' }
        { name: 'ASC_FEEDBACK_CONTAINER', value: 'RecipeHistory' }
        { name: 'ASC_LEASE_CONTAINER', value: 'leases' }
        { name: 'ASC_ENVIRONMENT', value: 'dev' }
        { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
        { name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING', value: 'ClientId=${identity.properties.clientId};Authorization=AAD' }
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'ENABLE_ORYX_BUILD', value: 'true' }
        { name: 'WEBSITE_HEALTHCHECK_MAXPINGFAILURES', value: '3' }
      ]
    }
  }
  dependsOn: [hostStorageGrant, monitoringGrant]
}

resource ftpPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: app
  name: 'ftp'
  properties: { allow: false }
}

module vnetIntegration 'site-vnet.bicep' = if (!empty(integrationSubnetResourceId)) {
  name: '${namePrefix}-feedback-vnet'
  params: {
    appName: app.name
    integrationSubnetResourceId: integrationSubnetResourceId
  }
}

resource scmPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: app
  name: 'scm'
  properties: { allow: false }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'feedback-host'
  scope: app
  properties: {
    workspaceId: logAnalyticsResourceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      { category: 'FunctionAppLogs', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

output appName string = app.name
output appResourceId string = app.id
output principalId string = identity.properties.principalId
output clientId string = identity.properties.clientId
output storageResourceId string = storage.id
