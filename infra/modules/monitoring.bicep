@description('Approved monitoring resource location.')
param location string

@description('Project and environment-specific resource prefix.')
param namePrefix string

param tags object

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-logs'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    features: {
      disableLocalAuth: true
      enableLogAccessUsingOnlyResourcePermissions: false
      immediatePurgeDataOn30Days: true
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${namePrefix}-insights'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    DisableLocalAuth: true
    DisableIpMasking: false
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// These tables otherwise have special retention defaults rather than inheriting the workspace.
var applicationTables = [
  'AppAvailabilityResults'
  'AppBrowserTimings'
  'AppDependencies'
  'AppEvents'
  'AppExceptions'
  'AppMetrics'
  'AppPageViews'
  'AppPerformanceCounters'
  'AppRequests'
  'AppSystemEvents'
  'AppTraces'
]

resource tableRetention 'Microsoft.OperationalInsights/workspaces/tables@2023-09-01' = [for tableName in applicationTables: {
  parent: workspace
  name: tableName
  properties: {
    plan: 'Analytics'
    retentionInDays: 30
    totalRetentionInDays: 30
  }
  dependsOn: [
    applicationInsights
  ]
}]

output logAnalyticsResourceId string = workspace.id
output applicationInsightsResourceId string = applicationInsights.id
