@description('Foundry account name; the project is a child of it.')
param accountName string

@description('Foundry project name.')
param projectName string

@description('Principal IDs of workload identities that invoke agents and models.')
param invokerPrincipalIds array

// Foundry User (formerly Azure AI User). Granted on the project only, never the subscription or group.
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: account
  name: projectName
}

resource invokers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principalId in invokerPrincipalIds: {
  scope: project
  name: guid(project.id, principalId, foundryUserRoleId)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
  }
}]
