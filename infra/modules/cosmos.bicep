@maxLength(44)
param accountName string
param location string
param tags object
param approvedIpRanges array
param allowAllPublicNetwork bool
param productWriterPrincipalIds array

var databaseName = 'sous-chef'
var productContainers = [
  {
    name: 'Users'
    paths: [
      '/userId/?'
    ]
    composites: []
  }
  {
    name: 'ChatHistory'
    paths: [
      '/userId/?'
      '/recordKind/?'
      '/conversationId/?'
      '/createdAt/?'
      '/turnIndex/?'
      '/status/?'
    ]
    composites: [
      [
        { path: '/recordKind', order: 'ascending' }
        { path: '/createdAt', order: 'descending' }
        { path: '/id', order: 'descending' }
      ]
      [
        { path: '/recordKind', order: 'ascending' }
        { path: '/conversationId', order: 'ascending' }
        { path: '/turnIndex', order: 'ascending' }
        { path: '/id', order: 'ascending' }
      ]
    ]
  }
  {
    name: 'RecipeHistory'
    paths: [
      '/userId/?'
      '/recordKind/?'
      '/createdAt/?'
      '/generatedRecipeId/?'
      '/occasionId/?'
      '/accepted/?'
      '/cookedAt/?'
      '/rating/?'
      '/feedbackRevision/?'
      '/behaviorRevision/?'
      '/sourceId/?'
      '/policyVersion/?'
      '/status/?'
    ]
    composites: [
      [
        { path: '/recordKind', order: 'ascending' }
        { path: '/createdAt', order: 'descending' }
        { path: '/id', order: 'descending' }
      ]
    ]
  }
]

resource account 'Microsoft.DocumentDB/databaseAccounts@2025-04-15' = {
  name: accountName
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    capabilities: [
      { name: 'EnableServerless' }
    ]
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    backupPolicy: {
      type: 'Continuous'
      continuousModeProperties: {
        tier: 'Continuous7Days'
      }
    }
    disableLocalAuth: true
    disableKeyBasedMetadataWriteAccess: true
    publicNetworkAccess: allowAllPublicNetwork || !empty(approvedIpRanges) ? 'Enabled' : 'Disabled'
    ipRules: [for ipRange in (allowAllPublicNetwork ? [] : approvedIpRanges): {
      ipAddressOrRange: ipRange
    }]
    networkAclBypass: 'None'
    isVirtualNetworkFilterEnabled: false
    virtualNetworkRules: []
    enableAutomaticFailover: false
    enableMultipleWriteLocations: false
    enableFreeTier: false
    enableAnalyticalStorage: false
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2025-04-15' = {
  parent: account
  name: databaseName
  properties: {
    resource: {
      id: databaseName
    }
  }
}

resource containers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2025-04-15' = [for container in productContainers: {
  parent: database
  name: container.name
  properties: {
    resource: {
      id: container.name
      partitionKey: {
        paths: ['/userId']
        kind: 'Hash'
        version: 2
      }
      indexingPolicy: {
        automatic: true
        indexingMode: 'consistent'
        includedPaths: [for path in container.paths: { path: path }]
        excludedPaths: [{ path: '/*' }]
        compositeIndexes: container.composites
      }
    }
  }
}]

resource leases 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2025-04-15' = {
  parent: database
  name: 'leases'
  properties: {
    resource: {
      id: 'leases'
      partitionKey: {
        paths: ['/id']
        kind: 'Hash'
        version: 2
      }
      indexingPolicy: {
        automatic: true
        indexingMode: 'consistent'
        includedPaths: [{ path: '/*' }]
        excludedPaths: []
      }
    }
  }
}

// Container-scoped grants deliberately exclude lease storage.
resource metadataReader 'Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions@2025-04-15' = {
  parent: account
  name: guid(account.id, 'sdk-metadata-reader')
  properties: {
    roleName: 'Sous Chef SDK Metadata Reader'
    type: 'CustomRole'
    assignableScopes: [account.id]
    permissions: [{
      dataActions: ['Microsoft.DocumentDB/databaseAccounts/readMetadata']
    }]
  }
}

resource metadataGrants 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2025-04-15' = [for principalId in productWriterPrincipalIds: {
  parent: account
  name: guid(account.id, principalId, 'sdk-metadata-reader')
  properties: {
    principalId: principalId
    roleDefinitionId: metadataReader.id
    scope: account.id
  }
}]

resource writers 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2025-04-15' = [for grant in flatten(map(productWriterPrincipalIds, principalId => map(productContainers, container => {
  principalId: principalId
  containerName: container.name
}))): {
  parent: account
  name: guid(account.id, grant.principalId, grant.containerName, 'product-writer')
  properties: {
    principalId: grant.principalId
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: '${account.id}/dbs/${databaseName}/colls/${grant.containerName}'
  }
  dependsOn: [
    containers
  ]
}]

output accountResourceId string = account.id
output endpoint string = account.properties.documentEndpoint
output databaseName string = databaseName
