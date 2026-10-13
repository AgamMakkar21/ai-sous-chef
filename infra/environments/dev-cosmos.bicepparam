using '../cosmos.bicep'

param environment = 'dev'
param location = readEnvironmentVariable('ASC_COSMOS_LOCATION')
param owner = 'AgamMakkar21'
// Access is denied until approved ranges and principals are passed at deployment.
param approvedIpRanges = json(readEnvironmentVariable('ASC_COSMOS_APPROVED_IP_RANGES', '[]'))
param allowAllPublicNetwork = json(readEnvironmentVariable('ASC_COSMOS_ALLOW_ALL_PUBLIC_NETWORK', 'false'))
param productWriterPrincipalIds = json(readEnvironmentVariable('ASC_COSMOS_WRITER_PRINCIPAL_IDS', '[]'))
