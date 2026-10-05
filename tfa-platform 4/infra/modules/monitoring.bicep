// Log Analytics + Application Insights. Splunk ingestion happens downstream
// via the org's existing LAW->Event Hub->Splunk forwarder (see docs).
param suffix string
param location string
param tags object

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'law-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 90
  }
}

resource appi 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${suffix}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: law.id
    IngestionMode: 'LogAnalytics'
  }
}

output workspaceId string = law.id
output appInsightsConnectionString string = appi.properties.ConnectionString
