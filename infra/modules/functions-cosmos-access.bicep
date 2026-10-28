param accountName string
param principalId string

resource account 'Microsoft.DocumentDB/databaseAccounts@2025-04-15' existing = {
  name: accountName
}

resource metadata 'Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions@2025-04-15' = {
  parent: account
  name: guid(account.id, 'feedback-metadata-reader')
  properties: {
    roleName: 'Sous Chef Feedback Metadata Reader'
    type: 'CustomRole'
    assignableScopes: [account.id]
    permissions: [{
      dataActions: ['Microsoft.DocumentDB/databaseAccounts/readMetadata']
    }]
  }
}

resource metadataGrant 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2025-04-15' = {
  parent: account
  name: guid(account.id, principalId, 'feedback-metadata-reader')
  properties: {
    principalId: principalId
    roleDefinitionId: metadata.id
    scope: account.id
  }
}

resource sourceReader 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2025-04-15' = {
  parent: account
  name: guid(account.id, principalId, 'feedback-source-reader')
  properties: {
    principalId: principalId
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000001'
    scope: '${account.id}/dbs/sous-chef/colls/RecipeHistory'
  }
}

resource leaseWriter 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2025-04-15' = {
  parent: account
  name: guid(account.id, principalId, 'feedback-lease-writer')
  properties: {
    principalId: principalId
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: '${account.id}/dbs/sous-chef/colls/leases'
  }
}
