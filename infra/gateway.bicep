param gatewayName string

param litellmUrl string

@secure()
param litellmMasterKey string

param enableGateway bool = false

param gatewayCallerObjectId string = ''

param modelName string = 'gpt-4o'

var aliasName = '${modelName}-litellm'

#disable-next-line BCP081
resource gateway 'Microsoft.ApiManagement/aigateways@2025-09-01-preview' existing = {
  name: gatewayName
}

#disable-next-line BCP081
resource workspace 'Microsoft.ApiManagement/aigateways/workspaces@2025-09-01-preview' existing = {
  parent: gateway
  name: 'default'
}

#disable-next-line BCP081
resource litellmProvider 'Microsoft.ApiManagement/aigateways/workspaces/modelProviders@2025-09-01-preview' = if (enableGateway) {
  parent: workspace
  name: 'litellm'
  properties: {
    kind: 'Custom'
    name: 'LiteLLM'
    custom: {
      endpoint: litellmUrl
      authentication: {
        kind: 'ApiKey'
        apiKey: {
          headerName: 'Authorization'
          value: 'Bearer ${litellmMasterKey}'
        }
      }
    }
  }
}

#disable-next-line BCP081
resource litellmModel 'Microsoft.ApiManagement/aigateways/workspaces/modelProviders/models@2025-09-01-preview' = if (enableGateway) {
  parent: litellmProvider
  name: modelName
  properties: {
    name: modelName
    deployment: {
      modelName: modelName
    }
    supportedEndpoints: [
      '/v1/chat/completions'
    ]
  }
}

#disable-next-line BCP081
resource litellmAlias 'Microsoft.ApiManagement/aigateways/workspaces/aliases@2025-09-01-preview' = if (enableGateway) {
  parent: workspace
  name: aliasName
  properties: {
    aliasName: aliasName
    description: 'LiteLLM-backed ${modelName} model'
    backendModels: [
      {
        resourceId: litellmModel.id
        priority: 0
        weight: 1
      }
    ]
    selectionOrder: [
      'priority'
    ]
  }
}

var gatewayModelsUserRoleId = 'a6b68e3d-a1f0-4c60-8678-237c050cf4ea'

resource gatewayCallerRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableGateway && !empty(gatewayCallerObjectId)) {
  name: guid(gateway.id, gatewayCallerObjectId, gatewayModelsUserRoleId)
  scope: gateway
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      gatewayModelsUserRoleId
    )
    principalId: gatewayCallerObjectId
    principalType: 'User'
  }
}

output gatewayUrl string = enableGateway
  ? litellmAlias!.properties.gatewayEndpoints[0].baseUrl
  : ''
output gatewayAlias string = enableGateway ? aliasName : ''
