// ORIGIN: AI — template typed by Claude Code, reviewed by Kiel. The exposure choices (only the
//   deployer's address may connect, key-only login, a daily shutdown) are Claude Code's proposals
//   until Kiel adopts them. Never deployed by its author: it is checked offline (compiled, linted,
//   and tested for these rules), and needs an Azure subscription to run.
//
// One Ubuntu VM that runs the same docker-compose.yml as a laptop. Nothing else: no container
// service, no managed database, no Azure OpenAI. The data arrives as a dump (see deploy.sh).

targetScope = 'resourceGroup'

@description('The one address (or small network) allowed to reach the VM, such as 203.0.113.7/32. There is deliberately NO default: a deployment must say who may connect.')
param allowedSourceIp string

@description('Your SSH public key. Password login is switched off.')
param sshPublicKey string

param location string = resourceGroup().location
param vmName string = 'vm-clinical-context'

@description('16 GiB or more: HAPI, Postgres and a 4B model share the memory. More CPUs make the summary faster; there is no GPU.')
param vmSize string = 'Standard_D4s_v5'

param adminUsername string = 'azureuser'

@description('Time of day the VM stops itself (HHmm), so it cannot be left running by accident.')
param shutdownTime string = '2000'

param shutdownTimeZone string = 'UTC'

var dnsLabel = 'ctx-${uniqueString(resourceGroup().id)}'

// Only three ports are ever opened, and only to allowedSourceIp. The model and the database are
// never published by the compose file, so no rule for them exists.
resource nsg 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: '${vmName}-nsg'
  location: location
  properties: {
    securityRules: [
      {
        name: 'ssh'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: allowedSourceIp
          sourcePortRange: '0-65535'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRange: '22'
        }
      }
      {
        name: 'api'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: allowedSourceIp
          sourcePortRange: '0-65535'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRange: '8000'
        }
      }
      {
        name: 'hapi'
        properties: {
          priority: 120
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: allowedSourceIp
          sourcePortRange: '0-65535'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRange: '8080'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: '${vmName}-vnet'
  location: location
  properties: {
    addressSpace: { addressPrefixes: [ '10.10.0.0/16' ] }
    subnets: [
      {
        name: 'default'
        properties: {
          addressPrefix: '10.10.1.0/24'
          networkSecurityGroup: { id: nsg.id }
        }
      }
    ]
  }
}

// A static address, so the source links in the reviewer page survive a restart.
resource publicIp 'Microsoft.Network/publicIPAddresses@2023-11-01' = {
  name: '${vmName}-ip'
  location: location
  sku: { name: 'Standard' }
  properties: {
    publicIPAllocationMethod: 'Static'
    dnsSettings: { domainNameLabel: dnsLabel }
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2023-11-01' = {
  name: '${vmName}-nic'
  location: location
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          privateIPAllocationMethod: 'Dynamic'
          subnet: { id: '${vnet.id}/subnets/default' }
          publicIPAddress: { id: publicIp.id }
        }
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-03-01' = {
  name: vmName
  location: location
  properties: {
    hardwareProfile: { vmSize: vmSize }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: 'latest'
      }
      // Measured on the laptop: 3.9 GB of Postgres, about 7 GB of images, 3.3 GB for the model,
      // and the dump while it is being restored. 64 GB leaves room.
      osDisk: {
        createOption: 'FromImage'
        diskSizeGB: 64
        managedDisk: { storageAccountType: 'Premium_LRS' }
      }
    }
    osProfile: {
      computerName: vmName
      adminUsername: adminUsername
      customData: loadFileAsBase64('cloud-init.yaml')
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: sshPublicKey
            }
          ]
        }
      }
    }
    networkProfile: { networkInterfaces: [ { id: nic.id } ] }
  }
}

// The cheapest safeguard: the VM stops itself every day. Compute charges stop; the disk and the
// address are still billed until the resource group is deleted (see teardown.sh).
resource autoShutdown 'Microsoft.DevTestLab/schedules@2018-09-15' = {
  name: 'shutdown-computevm-${vmName}'
  location: location
  properties: {
    status: 'Enabled'
    taskType: 'ComputeVmShutdownTask'
    dailyRecurrence: { time: shutdownTime }
    timeZoneId: shutdownTimeZone
    targetResourceId: vm.id
    notificationSettings: { status: 'Disabled' }
  }
}

output publicIp string = publicIp.properties.ipAddress
output fqdn string = publicIp.properties.dnsSettings.fqdn
output adminUsername string = adminUsername
