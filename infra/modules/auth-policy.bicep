@description('Existing App Service. The app must have HTTPS-only hosting and no alternate ingress.')
param appName string

@allowed(['external_id', 'app_service'])
param authMode string = 'external_id'

param authIssuer string
param authClientId string

@description('Required in app_service mode. Contains tenant and client IDs only, never credentials.')
param socialAuth object = {}

resource app 'Microsoft.Web/sites@2024-04-01' existing = {
  name: appName
}

resource authentication 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: app
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: true
      runtimeVersion: '~1'
    }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'Return401'
      excludedPaths: concat(
        [
          '/health'
          '/ready'
          '/auth/login'
          '/auth/logout'
          '/auth/signed-out'
        ],
        authMode == 'app_service'
          ? [
              '/auth/login/aad'
              '/auth/login/google'
              '/auth/login/github'
            ]
          : []
      )
    }
    httpSettings: {
      requireHttps: true
    }
    identityProviders: union(
      {
        apple: { enabled: false }
        facebook: { enabled: false }
        twitter: { enabled: false }
        legacyMicrosoftAccount: { enabled: false }
        azureStaticWebApps: { enabled: false }
        azureActiveDirectory: {
          enabled: true
          registration: {
            clientId: authClientId
            clientSecretSettingName: 'MICROSOFT_PROVIDER_AUTHENTICATION_SECRET'
            openIdIssuer: authMode == 'app_service'
              ? '${environment().authentication.loginEndpoint}${socialAuth.tenantId}/v2.0'
              : authIssuer
          }
          validation: {
            allowedAudiences: [
              authClientId
            ]
            defaultAuthorizationPolicy: {
              allowedApplications: [
                authClientId
              ]
            }
          }
          login: {
            loginParameters: [
              'scope=openid profile email'
            ]
          }
        }
      },
      authMode == 'app_service'
        ? {
            google: {
              enabled: true
              registration: {
                clientId: socialAuth.googleClientId
                clientSecretSettingName: 'GOOGLE_PROVIDER_AUTHENTICATION_SECRET'
              }
              login: {
                scopes: [
                  'openid'
                  'profile'
                  'email'
                ]
              }
            }
            gitHub: {
              enabled: true
              registration: {
                clientId: socialAuth.githubClientId
                clientSecretSettingName: 'GITHUB_PROVIDER_AUTHENTICATION_SECRET'
              }
              login: {
                scopes: [
                  'read:user'
                ]
              }
            }
          }
        : {
            google: { enabled: false }
            gitHub: { enabled: false }
          }
    )
    login: {
      preserveUrlFragmentsForLogins: false
      allowedExternalRedirectUrls: []
      cookieExpiration: {
        convention: 'FixedTime'
        timeToExpiration: '01:00:00'
      }
      tokenStore: {
        enabled: true
        tokenRefreshExtensionHours: 0
      }
    }
  }
}
