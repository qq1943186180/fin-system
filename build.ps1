[CmdletBinding()]
param(
    [string]$PythonPath = "",
    [string]$PublishRoot = "",
    [switch]$UpdateDesktopShortcut
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
if (-not $PublishRoot) {
    $PublishRoot = Join-Path $ProjectRoot "release"
}
$PublishRoot = [IO.Path]::GetFullPath($PublishRoot)
# Keep this script ASCII so Windows PowerShell 5.1 does not misdecode UTF-8
# source without a BOM.  U+8D22 U+52A1 U+7CFB U+7EDF spells the product name.
$ReleaseName = -join ([char[]](0x8D22, 0x52A1, 0x7CFB, 0x7EDF))
$ReleaseDir = Join-Path $PublishRoot $ReleaseName
$BuildId = [guid]::NewGuid().ToString("N")
$TempRoot = Join-Path ([IO.Path]::GetTempPath()) "FinanceSystemBuild-$BuildId"
$VenvDir = Join-Path $TempRoot "venv"
$DistDir = Join-Path $TempRoot "dist"
$WorkDir = Join-Path $TempRoot "work"
$SelfCheckHome = Join-Path $TempRoot "self-check-data"
$SelfCheckReport = Join-Path $TempRoot "self-check.json"
$BuildSpec = Join-Path $TempRoot "finance-system.spec"
$Incoming = Join-Path $PublishRoot ".incoming-$BuildId"
$Previous = Join-Path $PublishRoot ".previous-$BuildId"

function Resolve-Python311 {
    param([string]$ExplicitPath)
    if ($ExplicitPath) {
        $candidate = [IO.Path]::GetFullPath($ExplicitPath)
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "Python executable does not exist: $candidate"
        }
    } else {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($launcher) {
            $candidate = (& $launcher.Source -3.11 -c "import sys; print(sys.executable)").Trim()
        } else {
            $command = Get-Command python -ErrorAction SilentlyContinue
            if (-not $command) {
                throw "Python 3.11 was not found. Pass -PythonPath explicitly."
            }
            $candidate = $command.Source
        }
    }
    $version = (& $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
    if ($version -ne "3.11") {
        throw "Python 3.11 is required; resolved $candidate ($version)."
    }
    return $candidate
}

function Assert-SafeChildPath {
    param([string]$Parent, [string]$Child)
    $resolvedParent = [IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $resolvedChild = [IO.Path]::GetFullPath($Child)
    if (-not $resolvedChild.StartsWith($resolvedParent, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing filesystem operation outside '$resolvedParent': $resolvedChild"
    }
}

function Test-ByteSequence {
    param([byte[]]$Content, [byte[]]$Needle)
    if (-not $Needle -or $Needle.Length -eq 0 -or $Content.Length -lt $Needle.Length) {
        return $false
    }
    for ($i = 0; $i -le $Content.Length - $Needle.Length; $i++) {
        if ($Content[$i] -ne $Needle[0]) { continue }
        $matched = $true
        for ($j = 1; $j -lt $Needle.Length; $j++) {
            if ($Content[$i + $j] -ne $Needle[$j]) {
                $matched = $false
                break
            }
        }
        if ($matched) { return $true }
    }
    return $false
}

function Assert-ReleaseIsClean {
    param([string]$Directory)
    $forbiddenNames = @("finance.db", "feishu.json", "credentials.json", ".env")
    $forbidden = Get-ChildItem -LiteralPath $Directory -Recurse -Force |
        Where-Object { $forbiddenNames -contains $_.Name -or $_.Name -eq ".venv" }
    if ($forbidden) {
        throw "Release contains mutable data or credentials: $($forbidden.FullName -join ', ')"
    }

    $liveConfig = Join-Path $ProjectRoot "config\feishu.json"
    if (-not (Test-Path -LiteralPath $liveConfig -PathType Leaf)) { return }
    try {
        $parsedConfig = Get-Content -Raw -LiteralPath $liveConfig -Encoding UTF8 | ConvertFrom-Json
        $secretProperty = $parsedConfig.PSObject.Properties['app_secret']
        if ($null -eq $secretProperty) { return }
        $secret = $secretProperty.Value
    } catch {
        throw "The live Feishu config could not be parsed for the release secret scan."
    }
    if ([string]::IsNullOrWhiteSpace([string]$secret)) { return }

    $needles = @(
        [Text.Encoding]::UTF8.GetBytes([string]$secret),
        [Text.Encoding]::Unicode.GetBytes([string]$secret)
    )
    foreach ($file in Get-ChildItem -LiteralPath $Directory -Recurse -File) {
        $content = [IO.File]::ReadAllBytes($file.FullName)
        foreach ($needle in $needles) {
            if (Test-ByteSequence -Content $content -Needle $needle) {
                throw "Release secret scan found the live Feishu secret in $($file.FullName)"
            }
        }
    }
}

function Write-HashManifest {
    param([string]$Directory)
    $manifest = Join-Path $Directory "manifest.sha256"
    $base = [IO.Path]::GetFullPath($Directory).TrimEnd('\') + '\'
    $lines = foreach ($file in Get-ChildItem -LiteralPath $Directory -Recurse -File |
        Where-Object { $_.FullName -ne $manifest } | Sort-Object FullName) {
        $relative = $file.FullName.Substring($base.Length).Replace('\', '/')
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $relative"
    }
    [IO.File]::WriteAllLines($manifest, $lines, [Text.UTF8Encoding]::new($false))
}

function Publish-ReleaseAtomically {
    param([string]$SourceDirectory)
    New-Item -ItemType Directory -Force -Path $PublishRoot | Out-Null
    Assert-SafeChildPath -Parent $PublishRoot -Child $Incoming
    Assert-SafeChildPath -Parent $PublishRoot -Child $Previous
    Assert-SafeChildPath -Parent $PublishRoot -Child $ReleaseDir

    Copy-Item -LiteralPath $SourceDirectory -Destination $Incoming -Recurse
    $hadPrevious = Test-Path -LiteralPath $ReleaseDir
    try {
        if ($hadPrevious) {
            Move-Item -LiteralPath $ReleaseDir -Destination $Previous
        }
        Move-Item -LiteralPath $Incoming -Destination $ReleaseDir
    } catch {
        if ((Test-Path -LiteralPath $Previous) -and -not (Test-Path -LiteralPath $ReleaseDir)) {
            Move-Item -LiteralPath $Previous -Destination $ReleaseDir
        }
        throw
    }
    if (Test-Path -LiteralPath $Previous) {
        try {
            Remove-Item -LiteralPath $Previous -Recurse -Force
        } catch {
            Write-Warning "New release is active, but the previous release could not be removed: $Previous"
        }
    }
}

function Update-ShortcutAtomically {
    param([string]$Executable)
    $desktop = [Environment]::GetFolderPath("Desktop")
    if ([string]::IsNullOrWhiteSpace($desktop)) {
        throw "Windows Desktop folder could not be resolved."
    }
    $shortcutPath = Join-Path $desktop "$ReleaseName.lnk"
    $temporary = Join-Path $desktop ".$ReleaseName-$BuildId.tmp.lnk"
    $backup = Join-Path $desktop ".$ReleaseName-$BuildId.backup.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($temporary)
    $shortcut.TargetPath = $Executable
    $shortcut.WorkingDirectory = Split-Path -Parent $Executable
    $shortcut.IconLocation = "$Executable,0"
    $shortcut.Description = $ReleaseName
    $shortcut.Save()

    if (Test-Path -LiteralPath $shortcutPath) {
        [IO.File]::Replace($temporary, $shortcutPath, $backup, $true)
        Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
    } else {
        Move-Item -LiteralPath $temporary -Destination $shortcutPath
    }
}

$Python = Resolve-Python311 -ExplicitPath $PythonPath
New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null

try {
    Write-Host "[1/6] Creating isolated Python 3.11 build environment..."
    & $Python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "Creating the isolated virtual environment failed." }
    $BuildPython = Join-Path $VenvDir "Scripts\python.exe"
    & $BuildPython -m pip install --disable-pip-version-check --no-input `
        "pyinstaller==6.21.0" `
        "pyinstaller-hooks-contrib==2026.6" `
        "altgraph==0.17.5" `
        "packaging==25.0" `
        "pefile==2024.8.26" `
        "pywin32-ctypes==0.2.3" `
        "setuptools==79.0.1"
    if ($LASTEXITCODE -ne 0) { throw "Installing pinned build dependencies failed." }

    Write-Host "[2/6] Building the relative onedir bundle..."
    $oldHashSeed = $env:PYTHONHASHSEED
    $oldEpoch = $env:SOURCE_DATE_EPOCH
    $oldProjectRoot = $env:FINANCE_SYSTEM_PROJECT_ROOT
    $env:PYTHONHASHSEED = "0"
    $env:SOURCE_DATE_EPOCH = "315532800"
    $env:FINANCE_SYSTEM_PROJECT_ROOT = $ProjectRoot
    # PyInstaller on some Windows locales cannot reopen a non-ASCII spec file
    # from argv.  Use a temporary ASCII filename while keeping the tracked
    # specification path-independent.
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "$ReleaseName.spec") -Destination $BuildSpec
    (Get-Content $BuildSpec) -replace 'pathex=\[\]', "pathex=[r'$ProjectRoot']" | Set-Content $BuildSpec
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "run_finance.py") -Destination (Join-Path $TempRoot "run_finance.py")
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "app_paths.py") -Destination (Join-Path $TempRoot "app_paths.py")
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "logging_setup.py") -Destination (Join-Path $TempRoot "logging_setup.py")
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "security.py") -Destination (Join-Path $TempRoot "security.py")
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "db") -Destination (Join-Path $TempRoot "db") -Recurse
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "modules") -Destination (Join-Path $TempRoot "modules") -Recurse
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "gui") -Destination (Join-Path $TempRoot "gui") -Recurse
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "icon.ico") -Destination (Join-Path $TempRoot "icon.ico")
    Push-Location $ProjectRoot
    try {
        & $BuildPython -m PyInstaller --noconfirm --clean `
            --distpath $DistDir --workpath $WorkDir `
            $BuildSpec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
    } finally {
        Pop-Location
        $env:PYTHONHASHSEED = $oldHashSeed
        $env:SOURCE_DATE_EPOCH = $oldEpoch
        $env:FINANCE_SYSTEM_PROJECT_ROOT = $oldProjectRoot
    }

    $StagedRelease = Join-Path $DistDir $ReleaseName
    $StagedExe = Join-Path $StagedRelease "$ReleaseName.exe"
    if (-not (Test-Path -LiteralPath $StagedExe -PathType Leaf)) {
        throw "Expected executable was not produced: $StagedExe"
    }

    Write-Host "[3/6] Running the frozen runtime self-check..."
    $oldHome = $env:FINANCE_SYSTEM_HOME
    $env:FINANCE_SYSTEM_HOME = $SelfCheckHome
    $stderrLog = Join-Path $TempRoot "self-check-stderr.txt"
    try {
        $process = Start-Process -FilePath $StagedExe `
            -ArgumentList @("--self-check", "--self-check-report", $SelfCheckReport) `
            -Wait -PassThru -WindowStyle Hidden -RedirectStandardError $stderrLog
    } finally {
        $env:FINANCE_SYSTEM_HOME = $oldHome
    }
    Write-Host "DEBUG: exe=$StagedExe report=$SelfCheckReport stderr=$stderrLog exit=$($process.ExitCode)"
    if (Test-Path $stderrLog) { Get-Content $stderrLog -Raw }
    if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $SelfCheckReport)) {
        throw "Frozen runtime self-check failed with exit code $($process.ExitCode)."
    }
    $check = Get-Content -Raw -LiteralPath $SelfCheckReport -Encoding UTF8 | ConvertFrom-Json
    if (-not $check.ok) { throw "Frozen runtime self-check reported failure." }

    Write-Host "[4/6] Scanning release for mutable data and live secrets..."
    Assert-ReleaseIsClean -Directory $StagedRelease

    Write-Host "[5/6] Writing SHA-256 manifest and publishing atomically..."
    Write-HashManifest -Directory $StagedRelease
    Publish-ReleaseAtomically -SourceDirectory $StagedRelease

    if ($UpdateDesktopShortcut) {
        Write-Host "[6/6] Updating the Desktop shortcut atomically..."
        Update-ShortcutAtomically -Executable (Join-Path $ReleaseDir "$ReleaseName.exe")
    } else {
        Write-Host "[6/6] Desktop shortcut unchanged (pass -UpdateDesktopShortcut after validation)."
    }

    Write-Host "Release ready: $ReleaseDir"
} finally {
    Assert-SafeChildPath -Parent ([IO.Path]::GetTempPath()) -Child $TempRoot
    if (Test-Path -LiteralPath $TempRoot) {
        Remove-Item -LiteralPath $TempRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $Incoming) {
        Remove-Item -LiteralPath $Incoming -Recurse -Force
    }
}
