@description('Existing web or Function App. Does not replace app settings or authentication.')
param appName string

@description('Owner-approved existing subnet delegated to Microsoft.Web/serverFarms, in the app region.')
@minLength(1)
param integrationSubnetResourceId string

resource app 'Microsoft.Web/sites@2024-04-01' existing = {
  name: appName
}

resource integration 'Microsoft.Web/sites/networkConfig@2024-04-01' = {
  parent: app
  name: 'virtualNetwork'
  properties: {
    subnetResourceId: integrationSubnetResourceId
    swiftSupported: true
  }
}
