// infra/main.bicep — Travel Folio Analysis platform (enterprise)
// Scope: resource group (e.g. asg-tfa-<env>-scus-rg)
//
// Network posture per the enterprise review: NO public data-plane exposure.
// All PaaS data services publish private endpoints into the platform VNet;
// the Function App is VNet-integrated and reaches them over private DNS.
// SharePoint (Microsoft Graph) egress goes through the VNet's outbound path.

targetScope = 'resourceGroup'

@allowed(['dev', 'test', 'prod'])
param env string = 'dev'
param location string = resourceGroup().location
param namePrefix string = 'tfa'

@description('Address space for the platform VNet.')
param vnetAddressSpace string = '10.40.0.0/22'

@description('Object ID of the Entra group that becomes SQL admin.')
param sqlEntraAdminGroupObjectId string
param sqlEntraAdminGroupName string

@description('Enable customer-managed keys (Restricted-data control). Requires the Key Vault + key to pre-exist.')
param enableCmk bool = false
param cmkKeyVaultKeyUri string = ''

@description('LLM deployment to create in the Foundry/OpenAI account.')
param llmDeploymentName string = 'gpt-5.6'
param llmModelName string = 'gpt-5.6'
param llmModelVersion string = ''
param llmCapacity int = 50

var suffix = '${namePrefix}-${env}-${location}'
var tags = {
  workload: 'travel-folio-analysis'
  environment: env
  dataClassification: 'Restricted'
  managedBy: 'bicep'
}

// ---------------------------------------------------------------- identity
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${suffix}'
  location: location
  tags: tags
}

// ---------------------------------------------------------------- modules
module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: { suffix: suffix, location: location, tags: tags }
}

module network 'modules/network.bicep' = {
  name: 'network'
  params: {
    suffix: suffix
    location: location
    tags: tags
    addressSpace: vnetAddressSpace
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    suffix: suffix
    location: location
    tags: tags
    peSubnetId: network.outputs.peSubnetId
    dnsZoneIds: network.outputs.dnsZoneIds
    logAnalyticsId: monitoring.outputs.workspaceId
    enableCmk: enableCmk
    cmkKeyVaultKeyUri: cmkKeyVaultKeyUri
    uamiId: uami.id
  }
}

module data 'modules/data.bicep' = {
  name: 'data'
  params: {
    suffix: suffix
    location: location
    tags: tags
    peSubnetId: network.outputs.peSubnetId
    dnsZoneIds: network.outputs.dnsZoneIds
    logAnalyticsId: monitoring.outputs.workspaceId
    sqlEntraAdminGroupObjectId: sqlEntraAdminGroupObjectId
    sqlEntraAdminGroupName: sqlEntraAdminGroupName
  }
}

module ai 'modules/ai.bicep' = {
  name: 'ai'
  params: {
    suffix: suffix
    location: location
    tags: tags
    peSubnetId: network.outputs.peSubnetId
    dnsZoneIds: network.outputs.dnsZoneIds
    logAnalyticsId: monitoring.outputs.workspaceId
    llmDeploymentName: llmDeploymentName
    llmModelName: llmModelName
    llmModelVersion: llmModelVersion
    llmCapacity: llmCapacity
  }
}

module functions 'modules/functions.bicep' = {
  name: 'functions'
  params: {
    suffix: suffix
    location: location
    tags: tags
    uamiId: uami.id
    uamiClientId: uami.properties.clientId
    integrationSubnetId: network.outputs.appSubnetId
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    storageAccountName: storage.outputs.accountName
    settings: {
      TFA_ENVIRONMENT: env
      TFA_STORAGE_ACCOUNT_URL: storage.outputs.blobEndpoint
      TFA_QUEUE_ACCOUNT_URL: storage.outputs.queueEndpoint
      TFA_WORK_QUEUE: 'folio-extract'
      TFA_QUEUE_CONNECTION__queueServiceUri: storage.outputs.queueEndpoint
      TFA_QUEUE_CONNECTION__credential: 'managedidentity'
      TFA_QUEUE_CONNECTION__clientId: uami.properties.clientId
      TFA_DOCINTEL_ENDPOINT: ai.outputs.docIntelEndpoint
      TFA_LLM_PROVIDER: 'azure_openai'
      TFA_LLM_ENDPOINT: ai.outputs.foundryEndpoint
      TFA_LLM_DEPLOYMENT: llmDeploymentName
      TFA_SQL_SERVER: data.outputs.sqlServerFqdn
      TFA_SQL_DATABASE: data.outputs.sqlDatabaseName
      TFA_COSMOS_ENDPOINT: data.outputs.cosmosEndpoint
      TFA_USER_ASSIGNED_CLIENT_ID: uami.properties.clientId
      TFA_SHAREPOINT_SYNC_CRON: '0 */5 * * * *'
      TFA_EXTRACTION_ENGINE: 'loop'
      TFA_LANGSMITH_ENABLED: 'false'
    }
  }
}

module rbac 'modules/rbac.bicep' = {
  name: 'rbac'
  params: {
    principalId: uami.properties.principalId
    storageAccountName: storage.outputs.accountName
    cosmosAccountName: data.outputs.cosmosAccountName
    docIntelAccountName: ai.outputs.docIntelAccountName
    foundryAccountName: ai.outputs.foundryAccountName
  }
}

module events 'modules/events.bicep' = {
  name: 'events'
  params: {
    suffix: suffix
    tags: tags
    storageAccountId: storage.outputs.accountId
    functionAppId: functions.outputs.functionAppId
  }
}

output functionAppName string = functions.outputs.functionAppName
output uamiClientId string = uami.properties.clientId
output uamiPrincipalId string = uami.properties.principalId
output sqlServerFqdn string = data.outputs.sqlServerFqdn
