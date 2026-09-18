$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$buildRoot = Join-Path $repoRoot ".build"
$venvPython = Join-Path $buildRoot "venv\Scripts\python.exe"
$portable = Join-Path $repoRoot "portable"

if (-not (Test-Path -LiteralPath $venvPython)) {
    python -m venv (Join-Path $buildRoot "venv")
}

& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $repoRoot "requirements-build.txt")
& $venvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath $portable `
    --workpath (Join-Path $buildRoot "pyinstaller") `
    (Join-Path $repoRoot "LyricsTool.spec")

$dataFolder = Join-Path $portable "音乐歌词工具数据"
New-Item -ItemType Directory -Force -Path $dataFolder | Out-Null
Copy-Item -LiteralPath (Join-Path $repoRoot "USAGE.txt") -Destination (Join-Path $dataFolder "使用说明.txt") -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "THIRD_PARTY.md") -Destination (Join-Path $dataFolder "开源项目与许可证说明.md") -Force
$gpuRuntime = Join-Path $repoRoot "音乐歌词工具数据\GPU运行库"
if (Test-Path -LiteralPath $gpuRuntime) {
    Copy-Item -LiteralPath $gpuRuntime -Destination $dataFolder -Recurse -Force
}

$exe = Join-Path $portable "AAA音乐歌词工具.exe"
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash
"$hash  AAA音乐歌词工具.exe" | Set-Content -LiteralPath (Join-Path $dataFolder "SHA256SUMS.txt") -Encoding utf8
Write-Host "构建完成：$exe"
