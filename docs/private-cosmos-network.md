# Private Cosmos access using an existing VNet

When Cosmos public network access must stay disabled, outbound IP rules have no
effect. Do not retry enabling public access or seek an exemption as a deployment
workaround. Use regional App Service VNet integration plus a NoSQL private endpoint
and private DNS. Managed identities and the existing scoped data grants still
apply; networking does not grant data access.

`infra/cosmos-private-network.bicep` adds a Cosmos `Sql` private endpoint, a DNS
zone group, and VNet integration for the existing worker and web app. It references
owner-supplied subnets and an existing DNS zone; **it does not create or replace a
VNet, subnet, address range, DNS zone, NSG, route table, Cosmos account, app, or
managed identity**. The account remains an `existing` Bicep reference, so this
deployment cannot enable its public endpoint or change its business data.

## Required network-owner inputs

Reuse `ai-sous-chef-vnet` in `test-2` if its location and routing are suitable.
Do not assume suitability from its name alone.

| Input | Requirement |
| --- | --- |
| Worker integration subnet | Existing subnet delegated to `Microsoft.Web/serverFarms`, in the worker's region |
| Web integration subnet | Separate existing delegated subnet for the web app's separate plan, in the web app's region |
| Private endpoint subnet | Existing nondelegated subnet approved for private endpoints, separate from both integration subnets, with enough free addresses for Cosmos account/regional IPs |
| Private DNS zone | Existing `privatelink.documents.azure.com` zone linked to both integration VNets, or resolvable through approved custom DNS forwarding |
| Routing | Both integration subnets can reach all Cosmos private-endpoint IPs; NSGs/UDRs/firewalls permit HTTPS TCP 443 for the Python gateway-mode Cosmos client and DNS to the approved resolver |

Use separate integration subnets for these separate plans; do not assume the
existing subnet can be shared or change its delegation while it is in use.
B1 supports regional VNet integration, but subnet capacity, plan quota and policy
still need validation. VNet location must match the app integrating with it.
If the web app is elsewhere, it needs a subnet in its own region with approved
peering/routing and DNS to the private endpoint. This entrypoint does not create
peering. The private endpoint's region matches its subnet/VNet, not necessarily
the Cosmos account's region.

The network owner must supply/approve missing subnets, address ranges, delegation
and DNS links. No speculative CIDRs are prescribed here. If an appropriate Cosmos
private endpoint and zone group already exist, **reuse them**: skip this
entrypoint's endpoint creation and use the VNet integration commands below.
Do not create a second endpoint simply because its name differs.

## WSL discovery (read-only)

Run in the existing authenticated Azure CLI shell, with the approved subscription
selected. These commands do not make network changes.

```bash
RG="test-2"
VNET="ai-sous-chef-vnet"

az network vnet show --resource-group "$RG" --name "$VNET" \
  --query '{id:id,location:location,dnsServers:dhcpOptions.dnsServers,addressSpace:addressSpace.addressPrefixes}' \
  --output json

az network vnet subnet list --resource-group "$RG" --vnet-name "$VNET" \
  --query '[].{name:name,id:id,prefix:addressPrefix,prefixes:addressPrefixes,delegations:delegations[].serviceName,privateEndpoints:privateEndpoints[].id,nsg:networkSecurityGroup.id,routeTable:routeTable.id}' \
  --output json

az network private-endpoint list --resource-group "$RG" \
  --query '[].{name:name,subnet:subnet.id,connections:privateLinkServiceConnections}' \
  --output json

az cosmosdb show --resource-group "$RG" --name "$COSMOS" \
  --query '{publicNetworkAccess:publicNetworkAccess,connections:privateEndpointConnections}' \
  --output json

az network private-dns zone list \
  --query "[?name=='privatelink.documents.azure.com'].{name:name,id:id,resourceGroup:resourceGroup}" \
  --output table
```

A centrally owned DNS zone or endpoint may be in another resource group or
subscription and outside your read access. An empty list does not prove absence;
ask the network owner before creating a duplicate. Inspect the selected zone's
links with:

```bash
DNS_RG="<owner-supplied-zone-resource-group>"
az network private-dns link vnet list --resource-group "$DNS_RG" \
  --zone-name privatelink.documents.azure.com --output table
```

If custom DNS servers are configured on the VNet, a zone link alone is not enough:
the owner must configure resolution/forwarding through their Azure DNS resolver
path. Do not overwrite organization DNS settings with public resolvers.

## Preview and apply

Only proceed after the network owner confirms the topology. Keep the existing
`COSMOS`, `FUNCTION_NAME`, `WEB_APP` and `RESOURCE_OWNER` shell variables.
Use **existing subnet resource IDs**, not names of new proposed subnets.

```bash
NETWORK_REGION="<existing-private-endpoint-vnet-region>"
WORKER_SUBNET_ID="<approved-worker-integration-subnet-resource-id>"
WEB_SUBNET_ID="<approved-web-integration-subnet-resource-id>"
PE_SUBNET_ID="<approved-private-endpoint-subnet-resource-id>"
DNS_ZONE_ID="<existing-privatelink.documents.azure.com-zone-resource-id>"

NETWORK_PARAMETERS=(
  "location=$NETWORK_REGION"
  "owner=$RESOURCE_OWNER"
  "cosmosAccountName=$COSMOS"
  "functionAppName=$FUNCTION_NAME"
  "webAppName=$WEB_APP"
  "workerIntegrationSubnetResourceId=$WORKER_SUBNET_ID"
  "webIntegrationSubnetResourceId=$WEB_SUBNET_ID"
  "privateEndpointSubnetResourceId=$PE_SUBNET_ID"
  "privateDnsZoneResourceId=$DNS_ZONE_ID"
)

az deployment group what-if --resource-group "$RG" \
  --template-file infra/cosmos-private-network.bicep \
  --parameters "${NETWORK_PARAMETERS[@]}"
```

Inspect for only one intended private endpoint/zone group and two app integration
updates. Do not apply a preview that changes unrelated resources. Integrating an
app may restart it; schedule this dev change appropriately.

```bash
az deployment group create --resource-group "$RG" \
  --name ai-sous-chef-dev-cosmos-private-network --mode Incremental \
  --template-file infra/cosmos-private-network.bicep \
  --parameters "${NETWORK_PARAMETERS[@]}"
```

If reusing an existing approved endpoint/DNS setup instead, attach only the apps:

```bash
az webapp vnet-integration add --resource-group "$RG" --name "$FUNCTION_NAME" \
  --vnet "<worker-vnet-resource-id>" --subnet "$WORKER_SUBNET_ID"
az webapp vnet-integration add --resource-group "$RG" --name "$WEB_APP" \
  --vnet "<web-vnet-resource-id>" --subnet "$WEB_SUBNET_ID"
```

Do not add an integration if the app already uses a different required network;
review that change with the owner. The supplied Bicep references existing sites
and writes only `sites/networkConfig`, preserving settings, identities and auth.
On subsequent full worker deployments include
`integrationSubnetResourceId="$WORKER_SUBNET_ID"`. For web foundation deployments,
set `ASC_APP_SERVICE_INTEGRATION_SUBNET_RESOURCE_ID="$WEB_SUBNET_ID"` when using
`infra/environments/dev.bicepparam`, or pass
`appServiceIntegrationSubnetResourceId` explicitly. Empty defaults do not deploy
or deliberately remove a network integration.

## Permissions and private-only boundaries

The provisioning operator needs private-endpoint create/write and subnet
`join/action` permission on the approved endpoint subnet, site networking write
permission on both apps, and permission to join the existing private DNS zone.
Automatic Cosmos connection approval requires the appropriate
`Microsoft.DocumentDB/databaseAccounts/privateEndpointConnectionsApproval/action`
permission; otherwise the Cosmos owner must approve the connection. Network
Contributor at the approved network scope and Website Contributor at the app
scopes are practical starting points, but cross-group/central DNS permissions
must be supplied by the relevant owner. Do not give the app runtime identities or
GitHub pipeline network-management/RBAC administration roles. The normal delivery
pipeline deploys code only; this topology is an owner-applied infrastructure step.

DNS-zone linking is owned by the network team; this entrypoint deliberately does
not create links in central DNS. Private endpoint/NIC and DNS costs apply.
The host storage and web/worker inbound endpoints retain their previous
configuration. If policy also prohibits **their** public access, stop: private
storage endpoints/DNS, app ingress and a network-connected deployment runner
require additional design. VNet integration is outbound only; it does not make
the health or SCM endpoint private, and this change is not a claim that the
entire application is privately hosted.

By default only private-address application traffic follows integration. We do
not force Route All, replace DNS, or override network-owner NSGs/UDRs. If the
approved topology requires controlled public-destination or configuration/build
egress, arrange that routing with the network owner; do not turn off safeguards.

## Verify from the app network, not an ordinary WSL laptop

```bash
az webapp vnet-integration list --resource-group "$RG" --name "$FUNCTION_NAME"
az webapp vnet-integration list --resource-group "$RG" --name "$WEB_APP"

az cosmosdb show --resource-group "$RG" --name "$COSMOS" \
  --query '{publicNetworkAccess:publicNetworkAccess,disableLocalAuth:disableLocalAuth,connections:privateEndpointConnections}' \
  --output json
```

Require `publicNetworkAccess=Disabled`, `disableLocalAuth=true`, and an **Approved**
private endpoint connection. Inspect the endpoint NIC IPs and the zone's A records.
For each application, use its running app container console/SSH (not just the
GitHub runner, local WSL shell, or an unrelated Kudu process) to run:

```bash
COSMOS_HOST="<account-name>.documents.azure.com" python - <<'PY'
import ipaddress
import os
import socket

host = os.environ["COSMOS_HOST"]
addresses = {entry[4][0] for entry in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
if not addresses:
    raise SystemExit("Cosmos DNS returned no addresses.")
if not all(ipaddress.ip_address(address).is_private for address in addresses):
    raise SystemExit("Cosmos is not resolving exclusively to private addresses.")
print("Resolved addresses (compare with the private endpoint NIC):", sorted(addresses))
with socket.create_connection((host, 443), timeout=10):
    print("Cosmos TCP 443 is reachable from this app.")
PY
```

Compare resolved addresses with the actual endpoint NIC addresses: private DNS
and an open TCP port alone are not authenticated Cosmos data-access evidence.
Keep the normal `<account>.documents.azure.com` SDK endpoint; do not substitute
the `privatelink` hostname or a raw IP, which can break TLS/SDK behavior.

Run the existing web `/ready` check after deployment to exercise its managed-
identity Cosmos dependency. Check for network/authorization failures in the app
logs without logging documents or credentials. The worker's `/api/health` remains
**host-only**: it does not exercise Cosmos or prove feedback processing. Worker
Cosmos/lease data-plane acceptance still belongs to the later trigger smoke.
An ordinary laptop needs an approved VPN/ExpressRoute/DNS route to query private
Cosmos directly; adding its public IP will not help while public access is disabled.

References: [App Service regional VNet integration](https://learn.microsoft.com/azure/app-service/overview-vnet-integration),
[Cosmos private endpoints and DNS](https://learn.microsoft.com/azure/cosmos-db/how-to-configure-private-endpoints).
