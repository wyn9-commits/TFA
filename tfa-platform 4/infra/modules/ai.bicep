// Document Intelligence + Foundry (AIServices) accounts, private-only,
// Entra-only auth, with the LLM deployment.
param suffix string
param location string
param tags object
param peSubnetId string
param dnsZoneIds object
param logAnalyticsId string
param llmDeploymentName string
param llmModelName string
param llmModelVersion string
param llmCapacity int

resource docintel 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: 'di-${suffix}'
  location: location
  tags: tags
  kind: 'FormRecognizer'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: 'di-${suffix}'
    publicNetworkAccess: 'Disabled'
    disableLocalAuth: true
    networkAcls: { defaultAction: 'Deny' }
  }
}

resource foundry 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: 'aif-${suffix}'
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: 'aif-${suffix}'
    publicNetworkAccess: 'Disabled'
    disableLocalAuth: true
    networkAcls: { defaultAction: 'Deny' }
  }
}

resource llmDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: foundry
  name: llmDeploymentName
  sku: { name: 'GlobalStandard', capacity: llmCapacity }
  properties: {
    model: {
      format: 'OpenAI'
      name: llmModelName
      version: empty(llmModelVersion) ? null : llmModelVersion
    }
    versionUpgradeOption: 'OnceCurrentVersionExpired'
  }
}

resource peDi 'Microsoft.Network/privateEndpoints@2024-01-01' = {
  name: 'pe-di-${suffix}'
  location: location
  tags: tags
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'di'
        properties: { privateLinkServiceId: docintel.id, groupIds: ['account'] }
      }
    ]
  }
}

resource peDiDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = {
  parent: peDi
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      { name: 'cognitive', properties: { privateDnsZoneId: dnsZoneIds.cognitive } }
    ]
  }
}

resource peAif 'Microsoft.Network/privateEndpoints@2024-01-01' = {
  name: 'pe-aif-${suffix}'
  location: location
  tags: tags
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'aif'
        properties: { privateLinkServiceId: foundry.id, groupIds: ['account'] }
      }
    ]
  }
}

resource peAifDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = {
  parent: peAif
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      { name: 'openai', properties: { privateDnsZoneId: dnsZoneIds.openai } }
      { name: 'cognitive', properties: { privateDnsZoneId: dnsZoneIds.cognitive } }
    ]
  }
}

output docIntelEndpoint string = docintel.properties.endpoint
output docIntelAccountName string = docintel.name
output foundryEndpoint string = foundry.properties.endpoint
output foundryAccountName string = foundry.name
