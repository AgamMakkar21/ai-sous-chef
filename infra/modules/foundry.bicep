@description('Approved Foundry resource location.')
param location string

@description('Project and environment-specific resource prefix.')
param namePrefix string

param tags object

@description('Model deployments. Deployment names are the stable names the app is configured with.')
param deployments array

@description('Capacity in thousands of tokens per minute for each deployment.')
@minValue(1)
param capacity int = 50

var accountName = '${namePrefix}-foundry-${uniqueString(resourceGroup().id)}'
var projectName = '${namePrefix}-project'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: accountName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: projectName
    description: 'AI Sous Chef ${tags.environment} agents.'
  }
}

// Deployments on one account must be created one at a time.
@batchSize(1)
resource modelDeployments 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = [for item in deployments: {
  parent: account
  name: item.name
  sku: {
    name: item.sku
    capacity: capacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: item.model
      version: item.version
    }
  }
  dependsOn: [
    project
  ]
}]

output accountResourceId string = account.id
output accountName string = account.name
output projectName string = project.name
output projectResourceId string = project.id
output projectEndpoint string = 'https://${accountName}.services.ai.azure.com/api/projects/${projectName}'
output projectPrincipalId string = project.identity.principalId
