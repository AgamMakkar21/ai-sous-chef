using '../operations.bicep'

param environment = 'dev'
param location = 'eastus2'
param owner = 'AgamMakkar21'
param workspaceName = 'ai-sous-chef-dev-logs'
param applicationInsightsName = 'ai-sous-chef-dev-insights'
param routing = { mode: 'disabled' }
