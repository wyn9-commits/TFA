// VNet, subnets, and private DNS zones for all data-plane services.
param suffix string
param location string
param tags object
param addressSpace string

var zones = [
  'privatelink.blob.core.windows.net'
  'privatelink.queue.core.windows.net'
  'privatelink.database.windows.net'
  'privatelink.documents.azure.com'
  'privatelink.cognitiveservices.azure.com'
  'privatelink.openai.azure.com'
]

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: 'vnet-${suffix}'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [addressSpace] }
    subnets: [
      {
        name: 'snet-private-endpoints'
        properties: {
          addressPrefix: cidrSubnet(addressSpace, 24, 0)
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: 'snet-app-integration'
        properties: {
          addressPrefix: cidrSubnet(addressSpace, 24, 1)
          delegations: [
            {
              name: 'flex'
              properties: { serviceName: 'Microsoft.App/environments' }
            }
          ]
        }
      }
    ]
  }
}

resource dnsZones 'Microsoft.Network/privateDnsZones@2020-06-01' = [for z in zones: {
  name: z
  location: 'global'
  tags: tags
}]

resource links 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [for (z, i) in zones: {
  parent: dnsZones[i]
  name: 'link-${suffix}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}]

output vnetId string = vnet.id
output peSubnetId string = vnet.properties.subnets[0].id
output appSubnetId string = vnet.properties.subnets[1].id
output dnsZoneIds object = {
  blob: dnsZones[0].id
  queue: dnsZones[1].id
  sql: dnsZones[2].id
  cosmos: dnsZones[3].id
  cognitive: dnsZones[4].id
  openai: dnsZones[5].id
}
