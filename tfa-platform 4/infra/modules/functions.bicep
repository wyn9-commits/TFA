// Function App on Flex Consumption, UAMI-only, VNet-integrated.
param suffix string
param location string
param tags object
param uamiId string
param uamiClientId string
param integrationSubnetId string
param appInsightsConnectionString string
param storageAccountName string
param settings object

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-${suffix}'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: { tier: 'FlexConsumption', name: 'FC1' }
  properties: { reserved: true }
}

resource app 'Microsoft.Web/sites@2024-04-01' = {
  name: 'func-${suffix}'
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uamiId}': {} }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    virtualNetworkSubnetId: integrationSubnetId
    functionAppConfig: {
      runtime: { name: 'python', version: '3.11' }
      scaleAndConcurrency: { maximumInstanceCount: 40, instanceMemoryMB: 2048 }
      deployment: {
        storage: {
          type: 'blobContainer'
          value: 'https://${storageAccountName}.blob.core.windows.net/deployments'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: uamiId
          }
        }
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      appSettings: concat(
        [
          { name: 'AzureWebJobsStorage__accountName', value: storageAccountName }
          { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
          { name: 'AzureWebJobsStorage__clientId', value: uamiClientId }
          { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
        ],
        map(items(settings), s => { name: s.key, value: string(s.value) })
      )
    }
  }
}

output functionAppId string = app.id
output functionAppName string = app.name
