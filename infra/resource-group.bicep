targetScope = 'subscription'

@allowed([
  'dev'
  'prod'
])
param environment string

@description('Owner-approved Azure region for a NEW environment resource group.')
@minLength(1)
param location string

@description('Accountable resource owner or team.')
@minLength(1)
param owner string

resource environmentGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'ai-sous-chef-${environment}-rg'
  location: location
  tags: {
    project: 'ai-sous-chef'
    environment: environment
    owner: owner
    managedBy: 'bicep'
  }
}

output resourceGroupId string = environmentGroup.id
