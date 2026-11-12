param location string

param containerImage string
param foundryApiBase string
param foundryApiVersion string
param modelName string = 'gpt-4o'

@secure()
param litellmMasterKey string

param resourceNames object

param tags object

param enableAdminUi bool = false
param databaseHost string = ''

@secure()
param uiPassword string = ''

@secure()
param litellmSaltKey string = ''

var litellmConfig = replace(loadTextContent('../litellm/config.yaml'), 'gpt-4o', modelName)

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2026-03-01' = {
  name: resourceNames.logAnalytics
  location: location
  tags: tags
  properties: {
    retentionInDays: 30
    sku: {
      name: 'PerGB2018'
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource managedEnvironment 'Microsoft.App/managedEnvironments@2026-07-01' = {
  name: resourceNames.managedEnvironment
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

resource containerApp 'Microsoft.App/containerApps@2026-07-01' = {
  name: resourceNames.containerApp
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 4000
        transport: 'auto'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      secrets: concat(
        [
          {
            name: 'litellm-master-key'
            value: litellmMasterKey
          }
          {
            name: 'litellm-config'
            value: litellmConfig
          }
        ],
        enableAdminUi
          ? [
              {
                name: 'litellm-ui-password'
                value: uiPassword
              }
              {
                name: 'litellm-salt-key'
                value: litellmSaltKey
              }
            ]
          : []
      )
    }
    template: {
      containers: [
        {
          name: 'litellm'
          image: containerImage
          args: [
            '--config'
            '/etc/litellm/config.yaml'
            '--host'
            '0.0.0.0'
            '--port'
            '4000'
          ]
          resources: {
            cpu: 1
            memory: '2Gi'
          }
          env: concat(
            [
              {
                name: 'AZURE_API_BASE'
                value: foundryApiBase
              }
              {
                name: 'AZURE_API_VERSION'
                value: foundryApiVersion
              }
              {
                name: 'LITELLM_CONFIG_REVISION'
                value: uniqueString(litellmConfig)
              }
            ],
            [
              {
                name: 'LITELLM_MASTER_KEY'
                secretRef: 'litellm-master-key'
              }
            ],
            enableAdminUi
              ? [
                  {
                    name: 'AZURE_POSTGRESQL_AUTH'
                    value: 'true'
                  }
                  {
                    name: 'STORE_MODEL_IN_DB'
                    value: 'True'
                  }
                  {
                    name: 'DATABASE_HOST'
                    value: databaseHost
                  }
                  {
                    name: 'DATABASE_PORT'
                    value: '5432'
                  }
                  {
                    name: 'DATABASE_USER'
                    value: 'litellm_app'
                  }
                  {
                    name: 'DATABASE_NAME'
                    value: 'litellm'
                  }
                  {
                    name: 'DATABASE_SSLMODE'
                    value: 'verify-full'
                  }
                  {
                    name: 'DATABASE_SSLROOTCERT'
                    value: '/etc/ssl/certs/ca-certificates.crt'
                  }
                  {
                    name: 'UI_USERNAME'
                    value: 'admin'
                  }
                  {
                    name: 'UI_PASSWORD'
                    secretRef: 'litellm-ui-password'
                  }
                  {
                    name: 'LITELLM_SALT_KEY'
                    secretRef: 'litellm-salt-key'
                  }
                ]
              : []
          )
          volumeMounts: [
            {
              volumeName: 'litellm-config'
              mountPath: '/etc/litellm'
            }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: enableAdminUi ? '/health/readiness' : '/health/liveliness'
                port: 4000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: enableAdminUi ? 60 : 30
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health/liveliness'
                port: 4000
                scheme: 'HTTP'
              }
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 6
            }
          ]
        }
      ]
      volumes: [
        {
          name: 'litellm-config'
          storageType: 'Secret'
          secrets: [
            {
              secretRef: 'litellm-config'
              path: 'config.yaml'
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1
      }
    }
  }
}

output litellmUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output containerAppName string = containerApp.name
output containerAppPrincipalId string = containerApp.identity.principalId
