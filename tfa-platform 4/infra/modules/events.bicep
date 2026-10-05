// Event Grid: BlobCreated on the raw container -> enqueue_folio function.
param suffix string
param tags object
param storageAccountId string
param functionAppId string

resource topic 'Microsoft.EventGrid/systemTopics@2024-06-01-preview' = {
  name: 'egt-${suffix}'
  location: 'global'
  tags: tags
  properties: {
    source: storageAccountId
    topicType: 'Microsoft.Storage.StorageAccounts'
  }
}

resource sub 'Microsoft.EventGrid/systemTopics/eventSubscriptions@2024-06-01-preview' = {
  parent: topic
  name: 'folio-created'
  properties: {
    destination: {
      endpointType: 'AzureFunction'
      properties: {
        resourceId: '${functionAppId}/functions/enqueue_folio'
        maxEventsPerBatch: 1
      }
    }
    filter: {
      includedEventTypes: ['Microsoft.Storage.BlobCreated']
      subjectBeginsWith: '/blobServices/default/containers/travel-folios/'
    }
    retryPolicy: { maxDeliveryAttempts: 10, eventTimeToLiveInMinutes: 1440 }
  }
}
