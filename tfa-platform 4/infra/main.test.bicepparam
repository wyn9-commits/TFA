using './main.bicep'

param env = 'test'
// namePrefix drives every resource name. 'asgtfa' aligns with the resource
// group asg-tfa-<env>-scus-rg shown in the deployment architecture diagram.
param namePrefix = 'asgtfa'

// FILL IN: must not overlap any existing spoke.
param vnetAddressSpace = '10.40.0.0/22'

// FILL IN: az ad group show -g <group> --query id -o tsv
param sqlEntraAdminGroupObjectId = '00000000-0000-0000-0000-000000000000'
param sqlEntraAdminGroupName = 'sg-tfa-sql-admins-test'

// FILL IN: 'api://<api-client-id>' — the api:// form, NOT a bare client id.
param apiAudience = ''
param corsOrigins = ''

// Shared L2 cache: only needed above one API replica.
param enableRedis = false

// Customer-managed keys: required for Restricted data in prod.
param enableCmk = false

param llmDeploymentName = 'gpt-5.6'
param llmModelName = 'gpt-5.6'
param llmCapacity = 50
