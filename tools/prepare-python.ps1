# 准备内嵌 Python（把 vendor\python 造出来，供打包时随附）
#
# 为什么需要这个脚本：vendor\python 有 160+ MB，不适合进 git 仓库。
# 但打包又需要它，所以把「怎么造出来」固化成脚本 —— 仓库保持精简，
# 任何人 clone 后跑一次本脚本就能得到完全一样的运行环境。
#
# 用法：  powershell -ExecutionPolicy Bypass -File tools\prepare-python.ps1
# 或直接双击根目录的「准备内嵌Python.bat」
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $root 'requirements.txt'))) {
    # 脚本被单独拷走时兜底：以当前目录为准
    $root = (Get-Location).Path
}
Set-Location $root

$ver    = '3.14.7'
$arch   = 'amd64'
$url    = "https://registry.npmmirror.com/-/binary/python/$ver/python-$ver-embed-$arch.zip"
$vendor = Join-Path $root 'vendor'
$zip    = Join-Path $vendor "python-$ver-embed-$arch.zip"
$dest   = Join-Path $vendor 'python'
$spDir  = Join-Path $dest 'Lib\site-packages'
$pth    = Join-Path $dest 'python314._pth'

Write-Host "[1/5] 检查目录" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $vendor | Out-Null

Write-Host "[2/5] 下载 embeddable Python $ver ($arch)" -ForegroundColor Cyan
if (Test-Path $zip) {
    Write-Host ("      已存在，跳过下载 ({0:N2} MB)" -f ((Get-Item $zip).Length/1MB))
} else {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    Invoke-WebRequest -Uri $url -OutFile $zip -TimeoutSec 300 -UseBasicParsing
    $sw.Stop()
    Write-Host ("      完成 {0:N2} MB，用时 {1:N1}s" -f ((Get-Item $zip).Length/1MB), $sw.Elapsed.TotalSeconds)
}

Write-Host "[3/5] 解压 + 打开 site-packages" -ForegroundColor Cyan
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
Expand-Archive -Path $zip -DestinationPath $dest -Force
New-Item -ItemType Directory -Force -Path $spDir | Out-Null

# 关键：必须用无 BOM 的 UTF-8 写 ._pth。
# PowerShell 5.1 的 Set-Content -Encoding UTF8 会写 BOM，而 Python 的 ._pth
# 不剥 BOM，首行会变成 \ufeffpython314.zip，导致
# "Fatal Python error: Failed to import encodings module"。
$lines = @('python314.zip', '.', 'Lib\site-packages', 'import site')
$enc = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines($pth, $lines, $enc)
$b = [System.IO.File]::ReadAllBytes($pth)
if ($b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) {
    throw "._pth 仍带 BOM，请检查写入方式"
}

Write-Host "[4/5] 安装依赖到内置 Python" -ForegroundColor Cyan
$sysPy = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $sysPy) { throw "需要先用系统的 Python 来执行 pip（本机需有 Python 3.9+）" }
& python -m pip install --target $spDir --upgrade --no-warn-script-location `
    -i https://pypi.tuna.tsinghua.edu.cn/simple -r (Join-Path $root 'requirements.txt')
# 注：pip 有时因无关包的版本冲突往 stderr 写警告，PowerShell 会当成非零，
# 所以不看退出码，直接验证能否导入（下一步）。

Write-Host "[5/5] 精简 + 自检" -ForegroundColor Cyan
# cv2 仅 moment.py（朋友圈）用；imageio_ffmpeg 仅 media.py 转码图片且有 .wxgf 兜底。
# numpy 必须保留 —— accessibility.py / uia_driver.py（UIA 核心）在用。
foreach ($n in @('cv2', 'imageio_ffmpeg')) {
    $p = Join-Path $spDir $n
    if (Test-Path $p) { Remove-Item $p -Recurse -Force; Write-Host "      已删 $n" }
}
Get-ChildItem $spDir -Filter 'opencv_python*' -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item $_.FullName -Recurse -Force }

$py = Join-Path $dest 'python.exe'
$code = 'import sys' + "`n" +
        'mods=["PIL","psutil","uiautomation","win32clipboard","win32con","win32gui","win32ui","wechatauto","numpy"]' + "`n" +
        'bad=[]' + "`n" +
        'for m in mods:' + "`n" +
        '    try: __import__(m)' + "`n" +
        '    except Exception as e: bad.append((m,str(e)[:60]))' + "`n" +
        'print("失败:", bad if bad else "无")' + "`n" +
        'sys.exit(1 if bad else 0)' + "`n"
$tmpPy = Join-Path $env:TEMP '_wcs_imp.py'
[System.IO.File]::WriteAllText($tmpPy, $code, (New-Object System.Text.UTF8Encoding($false)))
& $py $tmpPy
$rc = $LASTEXITCODE
Remove-Item $tmpPy -Force -ErrorAction SilentlyContinue
if ($rc -ne 0) { throw "内置 Python 依赖导入失败，见上面的输出" }

$size = (Get-ChildItem $spDir -Recurse -File -ErrorAction SilentlyContinue |
         Measure-Object Length -Sum).Sum
Write-Host ("`n完成：内置 Python $ver 就绪，site-packages {0:N1} MB" -f ($size/1MB)) -ForegroundColor Green
Write-Host "位置: $dest" -ForegroundColor Green
Write-Host "现在可以运行 打包.bat 了" -ForegroundColor Green