param([string]$Installer = 'dist/TraceAtlas-0.1.0-setup-x64.exe')
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot
$installerPath = (Resolve-Path -LiteralPath $Installer).Path
$registryName = '{D5514E5F-7797-46C9-9E39-37B88D4383CC}_is1'
foreach ($registryRoot in @('HKCU:', 'HKLM:')) {
    if (Test-Path -LiteralPath "$registryRoot\Software\Microsoft\Windows\CurrentVersion\Uninstall\$registryName") {
        throw 'An existing TraceAtlas installation was found. Leave it unchanged; use an isolated Windows account for this check.'
    }
}
$checkRoot = Join-Path $repoRoot ('artifacts/installer-check-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$installPath = Join-Path $checkRoot '安装程序 App'
New-Item -ItemType Directory -Path $checkRoot | Out-Null
$report = [ordered]@{ scope = 'current Windows developer machine; not clean Windows acceptance'; installed = $false; repaired = $false; startup = $false; workerChain = $false; uninstalled = $false; projectPreserved = $false }
$appProcess = $null
try {
    foreach ($stage in @('install', 'repair')) {
        $logPath = Join-Path $checkRoot ($stage + '.log')
        $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOICONS', '/SP-',
            "/DIR=`"$installPath`"", "/LOG=`"$logPath`"")
        $setup = Start-Process -FilePath $installerPath -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
        if ($setup.ExitCode -ne 0) { throw "Installer $stage failed: $($setup.ExitCode)" }
        if ($stage -eq 'install') { $report.installed = $true } else { $report.repaired = $true }
    }
    $screenshot = Join-Path $checkRoot 'installed-startup.png'
    $appProcess = Start-Process -FilePath (Join-Path $installPath 'TraceAtlas.exe') -ArgumentList @('--screenshot', "`"$screenshot`"") -WindowStyle Hidden -PassThru
    $deadline = (Get-Date).AddSeconds(20)
    $imageComplete = $false
    while (-not $imageComplete -and -not $appProcess.HasExited -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 200
        if (Test-Path -LiteralPath $screenshot) {
            try {
                $pngBytes = [System.IO.File]::ReadAllBytes($screenshot)
                $imageComplete = $pngBytes.Length -gt 20 -and [System.BitConverter]::ToString($pngBytes, $pngBytes.Length - 8) -eq '49-45-4E-44-AE-42-60-82'
            } catch [System.IO.IOException] { $imageComplete = $false }
        }
    }
    if (-not $imageComplete) { throw 'Installed GUI did not finish writing its startup screenshot.' }
    $report.startup = $true
    $appProcess.Refresh()
    if ($appProcess.CloseMainWindow()) { $null = $appProcess.WaitForExit(10000) }
    if (-not $appProcess.HasExited) { $appProcess.Kill(); $appProcess.WaitForExit(); $report.startupCleanup = 'terminated owned idle smoke process' }
    $appProcess = $null
    $chainPath = Join-Path $checkRoot '工作流程 Project'
    uv run python scripts/check_bundle.py --bundle $installPath --output $chainPath
    if ($LASTEXITCODE -ne 0) { throw 'Installed worker workflow failed.' }
    $report.workerChain = $true
    $projectManifest = Join-Path $chainPath '验证项目/project.json'
    $beforeHash = (Get-FileHash -LiteralPath $projectManifest -Algorithm SHA256).Hash
    $uninstallLog = Join-Path $checkRoot 'uninstall.log'
    $uninstaller = Join-Path $installPath 'unins000.exe'
    $uninstall = Start-Process -FilePath $uninstaller -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=`"$uninstallLog`"") -WindowStyle Hidden -Wait -PassThru
    if ($uninstall.ExitCode -ne 0) { throw "Uninstall failed: $($uninstall.ExitCode)" }
    $report.uninstalled = -not (Test-Path -LiteralPath (Join-Path $installPath 'TraceAtlas.exe'))
    $report.projectPreserved = (Get-FileHash -LiteralPath $projectManifest -Algorithm SHA256).Hash -eq $beforeHash
    if (-not $report.uninstalled -or -not $report.projectPreserved) { throw 'Uninstall or project preservation verification failed.' }
    $report.passed = $true
} catch {
    $report.passed = $false
    $report.error = $_.Exception.Message
    throw
} finally {
    if ($appProcess -and -not $appProcess.HasExited) { $appProcess.Kill(); $appProcess.WaitForExit() }
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $checkRoot 'report.json') -Encoding utf8
    Write-Output "Installer report: $checkRoot"
}
