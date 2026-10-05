using './main.bicep'

param env = 'dev'
param namePrefix = 'tfa'
param vnetAddressSpace = '10.40.0.0/22'
// Entra group that administers SQL (replace with the real group):
param sqlEntraAdminGroupObjectId = '00000000-0000-0000-0000-000000000000'
param sqlEntraAdminGroupName = 'sg-tfa-sql-admins-dev'
param enableCmk = false            // enable in prod per Restricted controls
param llmDeploymentName = 'gpt-5.6'
param llmModelName = 'gpt-5.6'
param llmCapacity = 50
