$ErrorActionPreference = 'Stop'
$Root = Join-Path $env:TEMP 'ForgeTrace Native Picker Ω Fixture'
if (Test-Path $Root) { Remove-Item -Recurse -Force $Root }
$Deep = Join-Path $Root 'src\features\authentication\templates\forms\six\levels'
New-Item -ItemType Directory -Force -Path $Deep | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Root 'empty\nested') | Out-Null
Set-Content -Encoding UTF8 -Path (Join-Path $Root 'root.txt') -Value 'ForgeTrace Windows acceptance root'
Set-Content -Encoding UTF8 -Path (Join-Path $Deep 'sign-in.html') -Value '<form>ForgeTrace Windows acceptance</form>'
Set-Content -Encoding UTF8 -Path (Join-Path $Root '.env') -Value 'FORGETRACE_ACCEPTANCE_SECRET=preview-only'
$Hash = (Get-FileHash -Algorithm SHA256 (Join-Path $Deep 'sign-in.html')).Hash
Write-Host "Fixture created: $Root"
Write-Host "Deep file SHA-256: $Hash"
Write-Host 'Launch ForgeTrace and select this folder through Import local folder.'
