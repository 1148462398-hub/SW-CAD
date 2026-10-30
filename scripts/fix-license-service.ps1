<#
    修复 SolidWorks FlexNet 许可服务：
      1) 把服务指向稳定路径 C:\Program Files\SolidWorks_Flexnet_Server（不再依赖安装包目录 D:\xz\...）
      2) 设为开机自动启动
      3) 启动 SolidWorks Licensing Service
    必须以管理员身份运行。双击 run-fix-license.cmd 会自动提权。
#>

$ErrorActionPreference = 'Stop'

# ---- 必须管理员 ----
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[X] 需要管理员权限。请右键 run-fix-license.cmd -> 以管理员身份运行。" -ForegroundColor Red
    exit 1
}

$svcName = 'SolidWorks Flexnet Server'
$stable  = 'C:\Program Files\SolidWorks_Flexnet_Server'
$lmgrd   = Join-Path $stable 'lmgrd.exe'
$lic     = Join-Path $stable 'sw_d_SSQ.lic'
$log     = Join-Path $stable 'sw_d_SSQ.log'

Write-Host "=== SolidWorks 许可服务修复 ===" -ForegroundColor Cyan

# ---- 校验稳定路径文件齐全 ----
$need = @($lmgrd, $lic, (Join-Path $stable 'sw_d.exe'))
$missing = $need | Where-Object { -not (Test-Path $_) }
if ($missing) {
    Write-Host "[X] 稳定路径缺少文件：" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "    $_" }
    Write-Host "    请先把 _SolidSQUAD_\SolidWorks_Flexnet_Server 整个目录复制到 $stable"
    exit 1
}
Write-Host "[1/5] 稳定路径文件齐全：$stable" -ForegroundColor Green

# ---- 备份当前服务配置 ----
$old = (sc.exe qc $svcName 2>&1 | Select-String 'BINARY_PATH_NAME') -replace '.*:\s*', ''
Write-Host "[2/5] 原服务指向：$old"
$backupFile = Join-Path $PSScriptRoot 'license-service-backup.txt'
@(
    "备份时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
    "服务名: $svcName",
    "原可执行路径: $old",
    "修复后路径: $lmgrd"
) | Set-Content -Path $backupFile -Encoding UTF8
Write-Host "      已备份到 $backupFile"

# ---- 停止并删除旧服务 ----
Write-Host "[3/5] 停止并删除旧服务..."
if (Get-Service $svcName -ErrorAction SilentlyContinue) {
    & sc.exe stop $svcName  | Out-Null
    Start-Sleep -Seconds 2
    & sc.exe delete $svcName | Out-Null
    Start-Sleep -Seconds 2
}

# ---- 用 C 盘稳定路径重装服务（等价 server_install.bat，但不交互）----
Write-Host "[4/5] 用稳定路径重新安装服务..."
$installs = Join-Path $stable 'installs.exe'
& $installs -c $lic -e $lmgrd -l $log -n $svcName | Out-Null
Start-Sleep -Seconds 2

# 设为自动启动
& sc.exe config $svcName start= auto | Out-Null

# ---- 启动并验证 ----
Write-Host "[5/5] 启动服务并验证..."
& sc.exe start $svcName | Out-Null
Start-Sleep -Seconds 4

$svc = Get-Service $svcName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq 'Running') {
    Write-Host "[OK] $svcName 已运行，启动类型：" -NoNewline -ForegroundColor Green
    Write-Host ((sc.exe qc $svcName | Select-String 'START_TYPE'))
} else {
    Write-Host "[!] 服务未能启动，请检查 $log" -ForegroundColor Yellow
}

# SW 自带许可服务也一起拉起
$swlic = 'SolidWorks Licensing Service'
if (Get-Service $swlic -ErrorAction SilentlyContinue) {
    & sc.exe config $swlic start= auto | Out-Null
    & sc.exe start  $swlic | Out-Null
    Start-Sleep -Seconds 2
    Write-Host "[OK] $swlic -> $((Get-Service $swlic).Status)" -ForegroundColor Green
}

# ---- 用 lmutil 实际验证许可可领取 ----
Write-Host "`n=== 许可验证 ===" -ForegroundColor Cyan
& (Join-Path $stable 'lmutil.exe') lmstat -c 25734@localhost -a 2>&1 |
    Select-String 'license server UP|SW_D:|Users of solidworks|Total of' |
    Select-Object -First 8 |
    ForEach-Object { Write-Host $_ }

Write-Host "`n完成。服务现在指向稳定路径，且开机自动启动。" -ForegroundColor Cyan
Write-Host "以后即使删掉 D:\xz 安装包目录，许可也不会断。" -ForegroundColor Gray
