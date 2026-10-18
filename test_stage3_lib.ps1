# Helper library for test_stage3.ps1 (dot-sourced by it):
#
#   . "$PSScriptRoot\test_stage3_lib.ps1" -Port $Port
#
# It defines the HTTP helper, the server lifecycle helpers, and the
# report/counter functions the main test flow uses.

param([int]$Port = 8001)

$ProjectRoot = $PSScriptRoot
$Base  = "http://127.0.0.1:$Port"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$OutLog = Join-Path $env:TEMP "tripwise_stage3_out.log"
$ErrLog = Join-Path $env:TEMP "tripwise_stage3_err.log"
$PREFIX = "test-stage3-"

$script:pass = 0
$script:fail = 0
$script:skip = 0

function Report($label, $ok, $detail) {
  if ($ok) { $script:pass++; Write-Output "PASS  $label  $detail" }
  else     { $script:fail++; Write-Output "FAIL  $label  $detail" }
}

function Skip($label, $why) {
  $script:skip++; Write-Output "SKIP  $label  $why"
}

# Generic HTTP helper. Returns Code / Body / Json / Headers and never throws
# on 4xx/5xx: a WebException still carries the response we want to inspect.
function Invoke-Api($method, $path, $body, $origin) {
  $full = "$Base$path"
  $resp = $null
  try {
    $req = [System.Net.HttpWebRequest]::Create($full)
    $req.Method = $method
    $req.Timeout = 40000
    if ($method -eq "OPTIONS") {
      $req.Headers.Add("Origin", "http://example.com")
      $req.Headers.Add("Access-Control-Request-Method", "DELETE")
    } elseif ($origin) {
      $req.Headers.Add("Origin", $origin)
    }
    if ($null -ne $body) {
      $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
      $req.ContentType = "application/json; charset=utf-8"
      $req.ContentLength = $bytes.Length
      $stream = $req.GetRequestStream()
      $stream.Write($bytes, 0, $bytes.Length)
      $stream.Close()
    }
    $resp = $req.GetResponse()
  } catch [System.Net.WebException] {
    $resp = $_.Exception.Response
    if (-not $resp) {
      return @{ Code = 0; Body = $_.Exception.Message; Json = $null; Headers = $null }
    }
  }
  $code = [int]$resp.StatusCode
  $headers = $resp.Headers
  $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
  $text = $reader.ReadToEnd()
  $reader.Close()
  $resp.Close()
  $json = $null
  try { $json = $text | ConvertFrom-Json } catch { $json = $null }
  return @{ Code = $code; Body = $text; Json = $json; Headers = $headers }
}

function Has-ErrorShape($json) {
  # {"error": string, "detail": string} - the one Stage 3 error shape.
  return ($null -ne $json -and
          $json.error -is [string] -and $json.error.Length -gt 0 -and
          $json.detail -is [string] -and $json.detail.Length -gt 0)
}

# Start uvicorn in the background and poll /api/health until it answers.
# For the "database down" phase a deliberately unreachable DSN (nothing
# accepts connections on 127.0.0.1 port 1) is handed to the child only;
# this session's environment is restored as soon as the child exists.
function Start-TestServer([string]$dbUrl) {
  $hadDb = Test-Path env:DATABASE_URL
  $oldDb = $env:DATABASE_URL
  if ($dbUrl) { $env:DATABASE_URL = $dbUrl }
  try {
    $proc = Start-Process -FilePath $Python `
      -ArgumentList @("-m", "uvicorn", "main:app", "--port", "$Port", "--loop", "local_loop:local_loop_factory") `
      -WorkingDirectory $ProjectRoot `
      -RedirectStandardOutput $OutLog `
      -RedirectStandardError $ErrLog `
      -PassThru
  } finally {
    if ($hadDb) { $env:DATABASE_URL = $oldDb }
    else        { Remove-Item env:DATABASE_URL -ErrorAction SilentlyContinue }
  }
  for ($i = 0; $i -lt 60; $i++) {
    $r = Invoke-Api "GET" "/api/health"
    if ($r.Code -eq 200) { return $proc }
    Start-Sleep -Milliseconds 500
  }
  return $null
}

function Stop-TestServer($proc) {
  if ($proc -and -not $proc.HasExited) {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
  }
  for ($i = 0; $i -lt 40; $i++) {
    $busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $busy) { return }
    Start-Sleep -Milliseconds 250
  }
}

# Delete every row this script (or an earlier aborted run) created, matched
# by the test-stage3- destinationId prefix. Rows that are not ours stay.
function Remove-TestRows {
  $r = Invoke-Api "GET" "/api/itinerary"
  if ($r.Code -ne 200 -or -not $r.Json) { return -1 }
  $removed = 0
  foreach ($item in @($r.Json.data)) {
    if ("$($item.destinationId)".StartsWith($PREFIX)) {
      $d = Invoke-Api "DELETE" "/api/itinerary/$($item.id)"
      if ($d.Code -eq 200) { $removed++ }
    }
  }
  return $removed
}
