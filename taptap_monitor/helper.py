"""Windows login helper for servers whose IP TapTap's WAF blocks from the login endpoints.

The admin downloads a .bat that carries the server origin and a one-time pairing code
(never the admin token). Run on the admin's own PC, it performs the TapTap QR login and the
developer-console OAuth handshake from a residential IP, shows the QR in the admin page via
the server, and finally hands only the developer.taptap.cn cookies to the server.
The TapTap account session itself never leaves the PC.

The .bat re-reads itself and runs everything after the #PSSTART marker in PowerShell, so the
cmd part stays ASCII-only (cmd reads .bat files in the OEM code page).
"""

HELPER = r"""@echo off
title TapTap Login Helper
powershell -NoProfile -ExecutionPolicy Bypass -Command "$t=[IO.File]::ReadAllText('%~f0',[Text.Encoding]::UTF8); $m='#'+'PS'+'START'; iex $t.Substring($t.IndexOf($m))"
echo.
pause
exit /b
#PSSTART
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
$Server = '__ORIGIN__'
$PairCode = '__CODE__'
$XUA = 'V=1&PN=Accounts&LANG=zh_CN&VN_CODE=10&LOC=CN&PLT=PC&DS=Android&UID=' + [guid]::NewGuid() + '&OS=Windows&OSV=10&DT=PC'
$Web = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$Web.UserAgent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'

function Send-Request($Method, $Uri, $Body = $null, $Headers = @{}, [switch]$Json) {
    $params = @{ Uri = $Uri; Method = $Method; WebSession = $Web; UseBasicParsing = $true; Headers = $Headers }
    if ($Json) {
        $params.Body = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Compress))
        $params.ContentType = 'application/json; charset=utf-8'
    } elseif ($Body) { $params.Body = $Body }
    $text = $null
    try { $text = (Invoke-WebRequest @params).Content }
    catch {
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $text = $_.ErrorDetails.Message }
        elseif ($_.Exception.Response) {
            try { $text = (New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())).ReadToEnd() } catch {}
        }
        if (-not $text) { throw }
    }
    if ($text -is [byte[]]) { $text = [Text.Encoding]::UTF8.GetString($text) }
    try { return ($text | ConvertFrom-Json) }
    catch { throw ('返回了非 JSON 内容：' + $text.Substring(0, [Math]::Min(80, $text.Length))) }
}

function Invoke-Accounts($Method, $Path, $Body = $null, $Query = '') {
    $uri = 'https://accounts.taptap.cn' + $Path + '?X-UA=' + [uri]::EscapeDataString($XUA) + $Query
    Send-Request $Method $uri $Body @{ 'X-UA' = $XUA; 'x-requested-with' = 'XMLHttpRequest'; 'Origin' = 'https://accounts.taptap.cn' }
}

function Send-Report($Stage, $Extra = @{}) {
    $body = @{ stage = $Stage }
    foreach ($key in $Extra.Keys) { $body[$key] = $Extra[$key] }
    $answer = Send-Request 'POST' ($Server + '/api/console/pair/' + $PairCode) $body @{} -Json
    if (-not $answer.ok) { throw ('服务器拒绝：' + $answer.detail) }
}

try {
    Write-Host '正在向 TapTap 请求登录二维码…'
    $sid = [guid]::NewGuid().ToString()
    $device = Invoke-Accounts 'POST' '/api/device-code/get' @{ session_id = $sid; session_type = 'taptap' }
    if (-not $device.success) { throw ('获取二维码失败：' + $device.data.msg) }
    $interval = [Math]::Max(1, [int]$device.data.interval)
    $lifetime = [Math]::Max(60, [int]$device.data.expires_in)
    $deadline = (Get-Date).AddSeconds($lifetime)
    Send-Report 'qr' @{ qrcode_url = $device.data.qrcode_url; expires_in = $lifetime }
    Write-Host '二维码已显示在后台页面上，请用 TapTap App 扫码并确认登录。' -ForegroundColor Cyan
    $done = $false; $scanned = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds $interval
        $poll = Invoke-Accounts 'POST' '/api/device-code/login' @{ device_code = $device.data.device_code; session_id = $sid; session_type = 'taptap' }
        if ($poll.success) { $done = $true; break }
        $code = $poll.data.error
        if ($code -eq 'authorization_pending') { continue }
        if ($code -eq 'authorization_waiting') {
            if (-not $scanned) { $scanned = $true; Send-Report 'scanned'; Write-Host '已扫码，请在手机上确认…' }
            continue
        }
        if ($code -eq 'slow_down') { $interval = [Math]::Min(10, $interval * 2); continue }
        if ($code -eq 'access_denied') { throw '已在手机上取消登录' }
        throw ('登录失败：' + $code)
    }
    if (-not $done) { throw '二维码已过期，请回到后台重新下载登录助手' }

    Write-Host '登录成功，正在连接开发者后台…'
    $site = [uri]'https://developer.taptap.cn/'
    $login = Send-Request 'GET' 'https://developer.taptap.cn/api/auth/login/url?host=developer.taptap.cn&refer=%2Fv3%2F&path_prefix=%2Fv3'
    $query = ([uri]$login.data.url).Query.TrimStart('?')
    $grant = Invoke-Accounts 'GET' '/api/oauth2/auth/v2' $null ('&parameters=' + [uri]::EscapeDataString($query))
    if (-not $grant.success) { throw ('授权失败：' + $grant.data.msg) }
    $redirect = $grant.data.redirect_uri
    if (-not $redirect) {
        $type = $grant.data.grant_type; if (-not $type) { $type = 'authorization_code' }
        $approved = Invoke-Accounts 'POST' '/api/oauth2/approve/v2' @{ grant_type = $type; client_id = $grant.data.client.id; scopes = 'public_profile'; continuation_code = $grant.data.continuation_code; parameters = $query }
        if (-not $approved.success) { throw ('授权失败：' + $approved.data.msg) }
        $redirect = $approved.data.redirect_uri
    }
    $answer = @{}
    foreach ($part in ([uri]$redirect).Query.TrimStart('?').Split('&')) {
        $kv = $part.Split('=', 2)
        if ($kv.Count -eq 2) { $answer[$kv[0]] = [uri]::UnescapeDataString($kv[1]) }
    }
    if (-not $answer['code']) { throw '没有拿到开发者后台授权码' }
    $xsrf = ($Web.Cookies.GetCookies($site) | Where-Object { $_.Name -eq 'DC_XSRF_TOKEN' } | Select-Object -First 1).Value
    $session = Send-Request 'POST' 'https://developer.taptap.cn/api/auth/login' @{ host = 'developer.taptap.cn'; state = $answer['state']; code = $answer['code']; path_prefix = '/v3' } @{ 'X-DC-XSRF-TOKEN' = $xsrf } -Json
    if (-not $session.success) { throw ('开发者后台登录失败：' + $session.data.msg) }
    $cookie = ($Web.Cookies.GetCookies($site) | ForEach-Object { $_.Name + '=' + $_.Value }) -join '; '
    Send-Report 'done' @{ cookie = $cookie }
    Write-Host '完成！开发者后台已连接到服务器，可以关闭这个窗口了。' -ForegroundColor Green
} catch {
    Write-Host ('出错：' + $_.Exception.Message) -ForegroundColor Red
    try { Send-Report 'error' @{ message = $_.Exception.Message } } catch {}
}
"""


def helper_script(origin, code):
    # cmd needs CRLF line endings
    return HELPER.replace("__ORIGIN__", origin).replace("__CODE__", code).replace("\r\n", "\n").replace("\n", "\r\n")
