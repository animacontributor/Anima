# Run as Administrator if Windows Firewall blocks the local phone connection.
# This rule is limited to this Python, the configured TCP port and the local subnet.
# It does not change the network's Public/Private category or expose an Internet port.
param([string]$PhonePythonPath = '', [int]$PhonePortNumber = 0)
$ErrorActionPreference = 'Stop'
$phoneProjectRoot = Split-Path -Parent $PSScriptRoot
$phoneConfiguration = Join-Path $phoneProjectRoot 'engine\config.py'
if (-not $phonePythonPath) { $phonePythonPath = (& py -c 'import sys; print(sys.executable)').Trim() }
if (-not $phonePortNumber) { $phonePortNumber = 8780 }
$phoneConfigMatch = Select-String -LiteralPath $phoneConfiguration -Pattern '^PHONE_PORT\s*=\s*(\d+)'
if ($phoneConfigMatch -and -not $PSBoundParameters.ContainsKey('PhonePortNumber')) { $phonePortNumber = [int]$phoneConfigMatch.Matches[0].Groups[1].Value }
if (-not (Test-Path -LiteralPath $phonePythonPath)) { throw 'The project Python could not be found.' }
$phoneRule = Get-NetFirewallRule -Name 'AnimaPhoneWiFi' -ErrorAction SilentlyContinue
if ($phoneRule) { Remove-NetFirewallRule -Name 'AnimaPhoneWiFi' }
New-NetFirewallRule -Name 'AnimaPhoneWiFi' -DisplayName 'Anima Phone (local Wi-Fi)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $phonePortNumber -RemoteAddress LocalSubnet -Profile Any -Program $phonePythonPath | Out-Null
Write-Output "Anima Phone is allowed on the local subnet at TCP port $phonePortNumber."
