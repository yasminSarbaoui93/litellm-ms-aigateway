@description('Azure region for the PostgreSQL flexible server.')
param location string

@description('PostgreSQL flexible server name.')
param serverName string

@description('Application database name.')
param databaseName string = 'litellm'

@description('Major PostgreSQL engine version.')
param version string = '16'

@description('Object ID of the deploying Microsoft Entra user.')
param entraAdminObjectId string

@description('Display name or UPN of the deploying Microsoft Entra user.')
param entraAdminName string

@description('Resource tags applied to the server.')
param tags object

@description('Individual IPv4 addresses allowed to reach the public PostgreSQL endpoint.')
@minLength(1)
param allowedIps string[]

@description('App IP rules already verified to have identical start and end addresses; omit to reconcile every rule.')
param unchangedFirewallIps string[] = []

@description('Microsoft Entra tenant hosting the deploying administrator.')
param tenantId string = tenant().tenantId

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2026-10-01' = {
  name: serverName
  location: location
  tags: tags
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: version
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: tenantId
    }
    storage: {
      storageSizeGB: 32
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
  }
}

resource postgresAdministrator 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2026-10-01' = {
  parent: postgres
  name: entraAdminObjectId
  properties: {
    principalType: 'User'
    principalName: entraAdminName
    tenantId: tenantId
  }
}

resource postgresDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2026-10-01' = {
  parent: postgres
  name: databaseName
  dependsOn: [
    postgresAdministrator
  ]
}

@batchSize(1)
resource postgresFirewallRules 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2026-10-01' = [
  for ip in allowedIps: if (!contains(unchangedFirewallIps, ip)) {
    parent: postgres
    name: guid(serverName, ip)
    properties: {
      startIpAddress: ip
      endIpAddress: ip
    }
    dependsOn: [
      postgresTls
    ]
  }
]

resource postgresExtensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2026-10-01' = {
  parent: postgres
  name: 'azure.extensions'
  properties: {
    value: 'uuid-ossp,pgcrypto,pg_trgm'
    source: 'user-override'
  }
  dependsOn: [
    postgresDatabase
  ]
}

resource postgresTls 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2026-10-01' = {
  parent: postgres
  name: 'require_secure_transport'
  properties: {
    value: 'on'
    source: 'user-override'
  }
  dependsOn: [
    postgresExtensions
  ]
}

output databaseHost string = postgres.properties.fullyQualifiedDomainName
