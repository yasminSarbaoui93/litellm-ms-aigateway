targetScope = 'subscription'

@minLength(1)
@maxLength(40)
param environmentName string = 'litellm-demo'

param location string = 'swedencentral'

param resourceGroupName string

param containerImage string = 'ghcr.io/berriai/litellm@sha256:11175975a3093e05e92faee2fe2fe3412a3ae2fcaa849eb0fe25d5f0d8bcb2e9'

@secure()
param litellmMasterKey string

param enableGateway bool = false

param gatewayName string

param gatewayCallerObjectId string = ''

param containerAppName string = 'ca-${environmentName}'

param managedEnvironmentName string = 'cae-${environmentName}'

param logAnalyticsName string = 'log-${environmentName}'

param foundryAccountName string
param foundryResourceGroupName string = resourceGroupName
param foundryApiBase string = 'https://${foundryAccountName}.openai.azure.com/'
param foundryApiVersion string = '2024-10-21'
param modelName string = 'gpt-4o'

param enableDatabase bool = false
param enableAdminUi bool = false
param postgresServerName string = 'psql-${environmentName}'
param postgresLocation string = 'northeurope'
param postgresAllowedIps array = []
param postgresUnchangedFirewallIps array = []
param entraAdminObjectId string = ''
param entraAdminName string = ''

@secure()
param uiPassword string = ''

@secure()
param litellmSaltKey string = ''

var tags = {
  environment: environmentName
}

var resourceNames = {
  managedEnvironment: managedEnvironmentName
  containerApp: containerAppName
  logAnalytics: logAnalyticsName
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' existing = {
  name: resourceGroupName
}

resource foundryRg 'Microsoft.Resources/resourceGroups@2024-03-01' existing = {
  name: foundryResourceGroupName
}

module postgres './postgres.bicep' = if (enableDatabase) {
  name: 'litellm-postgres'
  scope: rg
  params: {
    location: postgresLocation
    serverName: postgresServerName
    databaseName: 'litellm'
    version: '16'
    entraAdminObjectId: entraAdminObjectId
    entraAdminName: entraAdminName
    allowedIps: postgresAllowedIps
    unchangedFirewallIps: postgresUnchangedFirewallIps
    tags: tags
  }
}

module resources './resources.bicep' = {
  name: 'litellm-resources'
  scope: rg
  params: {
    containerImage: containerImage
    location: location
    foundryApiBase: foundryApiBase
    foundryApiVersion: foundryApiVersion
    modelName: modelName
    litellmMasterKey: litellmMasterKey
    resourceNames: resourceNames
    tags: tags
    enableAdminUi: enableAdminUi
    databaseHost: enableAdminUi ? postgres!.outputs.databaseHost : ''
    uiPassword: uiPassword
    litellmSaltKey: litellmSaltKey
  }
}

module foundryAccess './foundry-access.bicep' = {
  name: 'litellm-foundry-access'
  scope: foundryRg
  params: {
    foundryAccountName: foundryAccountName
    principalId: resources.outputs.containerAppPrincipalId
  }
}

module gateway './gateway.bicep' = {
  name: 'litellm-gateway'
  scope: rg
  params: {
    enableGateway: enableGateway
    gatewayCallerObjectId: gatewayCallerObjectId
    gatewayName: gatewayName
    litellmMasterKey: litellmMasterKey
    litellmUrl: resources.outputs.litellmUrl
    modelName: modelName
  }
}

output litellmUrl string = resources.outputs.litellmUrl
output gatewayUrl string = gateway.outputs.gatewayUrl
output containerAppName string = resources.outputs.containerAppName
output containerAppPrincipalId string = resources.outputs.containerAppPrincipalId
output gatewayAlias string = gateway.outputs.gatewayAlias
output databaseHost string = enableDatabase ? postgres!.outputs.databaseHost : ''
