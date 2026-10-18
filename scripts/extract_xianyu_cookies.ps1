# extract_xianyu_cookies.ps1 - 从 Edge/Chrome SQLite 提取闲鱼 cookies → WSL
# 用 P/Invoke 直接调 DPAPI CryptUnprotectData（避免 C# 编译）

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Data.SQLite

# === P/Invoke DPAPI ===
Add-Type -Namespace WslCookie -Name DP -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("crypt32.dll", CharSet=System.Runtime.InteropServices.CharSet.Auto, SetLastError=true)]
public static extern bool CryptUnprotectData(
    ref DATA_BLOB pDataIn,
    System.Text.StringBuilder szDataDescr,
    ref DATA_BLOB pOptionalEntropy,
    System.IntPtr pvReserved,
    System.IntPtr hMsg,
    int dwFlags,
    ref DATA_BLOB pDataOut);
[System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
public struct DATA_BLOB {
    public int cbData;
    public System.IntPtr pbData;
}
'@

function Unprotect-Bytes([byte[]]$enc) {
    $a = New-Object WslCookie.DP+DATA_BLOB
    $a.cbData = $enc.Length
    $a.pbData = [System.Runtime.InteropServices.Marshal]::AllocHGlobal($enc.Length)
    [System.Runtime.InteropServices.Marshal]::Copy($enc, 0, $a.pbData, $enc.Length)
    $b = New-Object WslCookie.DP+DATA_BLOB
    if (-not [WslCookie.DP]::CryptUnprotectData([ref]$a, $null, [ref]$b, [System.IntPtr]::Zero, [System.IntPtr]::Zero, 0, [ref]$b)) {
        $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
        throw "CryptUnprotectData failed: $err"
    }
    $dec = New-Object byte[] $b.cbData
    [System.Runtime.InteropServices.Marshal]::Copy($b.pbData, $dec, 0, $b.cbData)
    [System.Runtime.InteropServices.Marshal]::FreeHGlobal($a.pbData)
    if ($b.pbData -ne [System.IntPtr]::Zero) { [System.Runtime.InteropServices.Marshal]::FreeHGlobal($b.pbData) }
    return [System.Text.Encoding]::UTF8.GetString($dec)
}

# === 找 cookie DB ===
$Browsers = @(
    @{ Name = "Edge";   CookiePath = "$env:LOCALAPPDATA\Microsoft\Edge\User Data\Default\Network\Cookies" },
    @{ Name = "Edge";   CookiePath = "$env:LOCALAPPDATA\Microsoft\Edge\User Data\Default\Cookies" },
    @{ Name = "Chrome"; CookiePath = "$env:LOCALAPPDATA\Google\Chrome\User Data\Default\Network\Cookies" },
    @{ Name = "Chrome"; CookiePath = "$env:LOCALAPPDATA\Google\Chrome\User Data\Default\Cookies" }
)
$DbPath = $null
foreach ($B in $Browsers) {
    if (Test-Path $B.CookiePath) { $DbPath = $B.CookiePath; Write-Host "Use $($B.Name): $DbPath"; break }
}
if (-not $DbPath) { Write-Error "找不到 Edge/Chrome cookies DB; 先在浏览器登录 https://www.goofish.com" }

# === Copy DB 避免锁冲突 ===
$TmpDb = Join-Path $env:TEMP "xianyu_cookies_$([Guid]::NewGuid()).db"
Copy-Item $DbPath $TmpDb -Force
try {
    $Conn = New-Object System.Data.SQLite.SQLiteConnection("Data Source=$TmpDb;Version=3;")
    $Conn.Open()
    $Cmd = $Conn.CreateCommand()
    $Cmd.CommandText = "SELECT name, host_key, path, encrypted_value FROM cookies WHERE host_key LIKE '%goofish%'"
    $Reader = $Cmd.ExecuteReader()
    $Records = @()
    while ($Reader.Read()) {
        $name = $Reader["name"]; $host = $Reader["host_key"]; $path = $Reader["path"]
        $enc = $Reader["encrypted_value"]
        if ($enc.Length -eq 0) { continue }
        try {
            $value = Unprotect-Bytes $enc
        } catch { Write-Warning "decrypt $name failed: $_"; continue }
        $Records += [PSCustomObject]@{ name=$name; value=$value; domain=$host; path=$path }
    }
    $Reader.Close(); $Conn.Close()
} finally { Remove-Item $TmpDb -ErrorAction SilentlyContinue }

Write-Host "Extracted $($Records.Count) xianyu cookies"
if ($Records.Count -eq 0) { Write-Error "No cookies extracted - 先在浏览器登录闲鱼" }

# === 写 WSL ===
wsl -d Ubuntu -- bash @"
mkdir -p /home/zw/.goofish-cli
"@

$TmpLocal = Join-Path $env:TEMP "xianyu_cookies_$([Guid]::NewGuid()).json"
$Records | ConvertTo-Json -Depth 3 | Set-Content $TmpLocal -Encoding UTF8

Copy-Item $TmpLocal "\\wsl$\Ubuntu\home\zw\.goofish-cli\cookies_raw.json" -Force
Remove-Item $TmpLocal -ErrorAction SilentlyContinue
Write-Host "Wrote cookies_raw.json to WSL"

# === WSL: 跑 goofish auth login + 看状态 ===
Write-Host "===WSL: goofish auth login==="
wsl -d Ubuntu -- bash @"
export PATH=/home/zw/.local/bin:\$PATH
goofish auth login /home/zw/.goofish-cli/cookies_raw.json
"@

Write-Host "===WSL: auth status==="
wsl -d Ubuntu -- bash @"
export PATH=/home/zw/.local/bin:\$PATH
goofish auth status
"@
