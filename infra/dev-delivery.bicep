targetScope = 'resourceGroup'

@description('Existing dev App Service; never a production app.')
param appName string

@description('Existing dev Foundry account.')
param foundryAccountName string

@description('Existing dev Foundry project.')
param foundryProjectName string

@description('Location of the deployment identity only.')
param location string

resource app 'Microsoft.Web/sites@2024-04-01' existing = {
  name: appName
}

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: account
  name: foundryProjectName
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'ai-sous-chef-dev-github-delivery'
  location: location
  tags: {
    project: 'ai-sous-chef'
    environment: 'dev'
    managedBy: 'bicep'
  }
}

// The dev environment's deployment branch policy must allow only refs/heads/dev.
resource federation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: identity
  name: 'github-dev'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: 'repo:AgamMakkar21@72168349/ai-sous-chef@1409242851:environment:dev'
    audiences: ['api://AzureADTokenExchange']
  }
}

resource appPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(app.id, identity.id, 'dev-app-publisher')
  scope: app
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'de139f84-1756-47ae-9be6-808fbbe84772')
  }
}

resource agentPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, identity.id, 'dev-agent-publisher')
  scope: project
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  }
}

output clientId string = identity.properties.clientId
output principalId string = identity.properties.principalId
output tenantId string = tenant().tenantId
