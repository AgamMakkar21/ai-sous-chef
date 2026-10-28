targetScope = 'resourceGroup'

@description('Existing app only. Does not redeploy Foundry, models, hosting, identities or RBAC.')
param appName string

@minLength(36)
@maxLength(36)
param authClientId string

@description('Nonsecret tenantId, googleClientId and githubClientId; secrets stay in existing app settings.')
param socialAuth object

resource app 'Microsoft.Web/sites@2024-04-01' existing = {
  name: appName
}

var previous = list('${app.id}/config/appsettings', '2024-04-01').properties
var retained = toObject(
  filter(
    items(previous),
    item =>
      !contains(
        [
          'ASC_AUTH_ISSUER'
          'ASC_AUTH_JWKS_URI'
          'ASC_AUTH_CLIENT_ID'
          'WEBSITE_AUTH_AAD_ALLOWED_TENANTS'
        ],
        item.key
      )
  ),
  item => item.key,
  item => item.value
)

module settings 'modules/app-settings.bicep' = {
  name: '${appName}-settings'
  params: {
    appName: appName
    settings: union(retained, {
      ASC_AUTH_MODE: 'app_service'
      ASC_AUTH_ORIGIN: 'https://${app.properties.defaultHostName}'
      ASC_AUTH_MICROSOFT_TENANT_ID: socialAuth.tenantId
      WEBSITE_AUTH_AAD_ALLOWED_TENANTS: socialAuth.tenantId
    })
  }
}

module authentication 'modules/auth-policy.bicep' = {
  name: '${appName}-authentication'
  params: {
    appName: appName
    authMode: 'app_service'
    authIssuer: ''
    authClientId: authClientId
    socialAuth: socialAuth
  }
  dependsOn: [settings]
}
