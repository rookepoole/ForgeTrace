param(
    [string]$OutputPath = "windows-v0522-git-write-acceptance.log"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Resolve-PythonCommand {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @{ Executable = "py"; Prefix = @("-3") }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @{ Executable = "python"; Prefix = @() }
    }
    throw "Python 3 was not found. Install Python 3.10 or newer and retry."
}

function Invoke-NativeCaptured {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,

        [string[]]$Arguments = @(),

        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory,

        [string]$AppendPath,

        [switch]$Echo
    )

    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    try {
        # Windows PowerShell 5.1 turns ordinary native stderr into ErrorRecord
        # objects. Python unittest writes its verbose progress to stderr even
        # when every test passes. Redirect both native streams at the process
        # boundary so $ErrorActionPreference = "Stop" cannot misclassify that
        # progress as a terminating NativeCommandError.
        $process = Start-Process `
            -FilePath $Executable `
            -ArgumentList $Arguments `
            -WorkingDirectory $WorkingDirectory `
            -NoNewWindow `
            -Wait `
            -PassThru `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath

        $captured = New-Object System.Collections.Generic.List[string]
        foreach ($streamPath in @($stdoutPath, $stderrPath)) {
            if (Test-Path -LiteralPath $streamPath) {
                foreach ($line in @(Get-Content -LiteralPath $streamPath -ErrorAction Stop)) {
                    [void]$captured.Add([string]$line)
                }
            }
        }

        if ($AppendPath) {
            $captured | Add-Content -LiteralPath $AppendPath -Encoding UTF8
        }
        if ($Echo) {
            foreach ($line in $captured) {
                Write-Host $line
            }
        }

        return [pscustomobject]@{
            ExitCode = [int]$process.ExitCode
            Lines = @($captured)
        }
    }
    finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

$python = Resolve-PythonCommand
$git = Get-Command git -ErrorAction Stop
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$output = if ([System.IO.Path]::IsPathRooted($OutputPath)) {
    [System.IO.Path]::GetFullPath($OutputPath)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $root $OutputPath))
}

Push-Location $root
try {
    $pythonVersionArgs = @($python.Prefix) + @("--version")
    $pythonVersion = Invoke-NativeCaptured `
        -Executable $python.Executable `
        -Arguments $pythonVersionArgs `
        -WorkingDirectory $root
    if ($pythonVersion.ExitCode -ne 0) {
        throw "Python version probe failed with exit code $($pythonVersion.ExitCode)."
    }

    $gitVersion = Invoke-NativeCaptured `
        -Executable $git.Source `
        -Arguments @("--version") `
        -WorkingDirectory $root
    if ($gitVersion.ExitCode -ne 0) {
        throw "Git version probe failed with exit code $($gitVersion.ExitCode)."
    }

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("ForgeTrace v0.5.2.2 Windows Git-write acceptance")
    $lines.Add("RecordedAt: $([DateTime]::UtcNow.ToString('o'))")
    $lines.Add("ComputerName: $env:COMPUTERNAME")
    $lines.Add("Windows: $([System.Environment]::OSVersion.VersionString)")
    $lines.Add("PowerShell: $($PSVersionTable.PSVersion)")
    $lines.Add("Python: $(($pythonVersion.Lines -join ' ').Trim())")
    $lines.Add("Git: $(($gitVersion.Lines -join ' ').Trim())")
    $drive = (Get-Item $root).PSDrive.Name
    $volume = Get-Volume -DriveLetter $drive -ErrorAction SilentlyContinue
    if ($volume) {
        $lines.Add("Filesystem: $($volume.FileSystem) on $drive`:")
    } else {
        $lines.Add("Filesystem: unavailable")
    }
    $lines.Add("")
    $lines | Set-Content -LiteralPath $output -Encoding UTF8

    $testArgs = @($python.Prefix) + @(
        "-m", "unittest", "-v",
        "tests.test_v052_transactional_git_writes",
        "tests.test_v0521_git_write_failure_injection",
        "tests.test_v0522_windows_acceptance_runner"
    )
    Write-Host "Running automated transactional Git-write acceptance suites..."
    $testResult = Invoke-NativeCaptured `
        -Executable $python.Executable `
        -Arguments $testArgs `
        -WorkingDirectory $root `
        -AppendPath $output `
        -Echo
    if ($testResult.ExitCode -ne 0) {
        throw "Automated Windows Git-write tests failed with exit code $($testResult.ExitCode)."
    }

    "" | Add-Content -LiteralPath $output -Encoding UTF8
    "AUTOMATED_RESULT: OK" | Add-Content -LiteralPath $output -Encoding UTF8
    "Manual owner-browser checklist remains required; see tests/WINDOWS_TRANSACTIONAL_GIT_WRITES_ACCEPTANCE.md." | Add-Content -LiteralPath $output -Encoding UTF8
    Write-Host "Automated acceptance passed. Evidence: $output"
}
finally {
    Pop-Location
}
