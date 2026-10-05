// Storage account: raw documents (immutable-by-policy container) + work queues.
// Public network access DISABLED; private endpoints for blob + queue.
param suffix string
param location string
param tags object
param peSubnetId string
param dnsZoneIds object
param logAnalyticsId string
param enableCmk bool
param cmkKeyVaultKeyUri string
param uamiId string

var name = replace('st${suffix}', '-', '')

resource sa 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: take(name, 24)
  location: location
  tags: tags
  sku: { name: 'Standard_ZRS' }
  kind: 'StorageV2'
  identity: enableCmk ? {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uamiId}': {} }
  } : null
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false          // Entra-only data plane
    publicNetworkAccess: 'Disabled'
    supportsHttpsTrafficOnly: true
    networkAcls: { defaultAction: 'Deny', bypass: 'None' }
    encryption: enableCmk ? {
      identity: { userAssignedIdentity: uamiId }
      keySource: 'Microsoft.Keyvault'
      keyvaultproperties: { keyvaulturi: '', keyname: '', keyvaultKeyUri: cmkKeyVaultKeyUri }
      services: {
        blob: { enabled: true }
        queue: { enabled: true }
      }
    } : null
  }
}

resource blobSvc 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: sa
  name: 'default'
  properties: {
    deleteRetentionPolicy: { enabled: true, days: 30 }
    containerDeleteRetentionPolicy: { enabled: true, days: 30 }
    isVersioningEnabled: true
  }
}

resource rawContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobSvc
  name: 'travel-folios'
  properties: { publicAccess: 'None' }
}

resource queueSvc 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' = {
  parent: sa
  name: 'default'
}

resource workQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = {
  parent: queueSvc
  name: 'folio-extract'
}

resource poisonQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = {
  parent: queueSvc
  name: 'folio-extract-poison'
}

resource peBlob 'Microsoft.Network/privateEndpoints@2024-01-01' = {
  name: 'pe-blob-${suffix}'
  location: location
  tags: tags
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'blob'
        properties: { privateLinkServiceId: sa.id, groupIds: ['blob'] }
      }
    ]
  }
}

resource peBlobDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = {
  parent: peBlob
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      { name: 'blob', properties: { privateDnsZoneId: dnsZoneIds.blob } }
    ]
  }
}

resource peQueue 'Microsoft.Network/privateEndpoints@2024-01-01' = {
  name: 'pe-queue-${suffix}'
  location: location
  tags: tags
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'queue'
        properties: { privateLinkServiceId: sa.id, groupIds: ['queue'] }
      }
    ]
  }
}

resource peQueueDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = {
  parent: peQueue
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      { name: 'queue', properties: { privateDnsZoneId: dnsZoneIds.queue } }
    ]
  }
}

resource diag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: sa
  name: 'to-law'
  properties: {
    workspaceId: logAnalyticsId
    metrics: [{ category: 'Transaction', enabled: true }]
  }
}

output accountName string = sa.name
output accountId string = sa.id
output blobEndpoint string = sa.properties.primaryEndpoints.blob
output queueEndpoint string = sa.properties.primaryEndpoints.queue
