param appName string

@secure()
param settings object

resource app 'Microsoft.Web/sites@2024-04-01' existing = {
  name: appName
}

resource appSettings 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: app
  name: 'appsettings'
  properties: settings
}
